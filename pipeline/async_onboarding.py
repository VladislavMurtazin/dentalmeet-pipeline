"""Async onboarding: presentation + form after interest, then ingest form replies."""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from .email_sender import send_email
from .to_sheets import connect, is_email

DEFAULT_PRESENTATION = "https://canva.link/n4hed1htcuepmq5"
DEFAULT_BRIEF_FORM = (
    "https://docs.google.com/forms/d/"
    "1-_DIAdCf2sqlhcwnUWsAYYKfAb2ibUddOhKwUCA5W_k/viewform"
)
DEFAULT_CALENDLY = "https://calendly.com/dentalmeet-agency/new-meeting/"

CLIENTS_HEADERS = [
    "Company",
    "Name",
    "Email",
    "ICP",
    "Geo",
    "Services",
    "Calendly",
    "Workspace Email",
    "Deal Size",
    "Pricing",
    "Notes",
    "Status",
    "Created",
    "Payment Status",
    "Payment TXID",
    "Payment Date",
    "Payment Amount",
]


def _col_letter(n: int) -> str:
    """1-based column index → Excel letter (1=A)."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _clients_range(row: int) -> str:
    return f"A{row}:{_col_letter(len(CLIENTS_HEADERS))}{row}"

# Flexible header aliases on the Form Responses sheet
FIELD_ALIASES = {
    "company": ("company name", "company"),
    "name": ("your name", "name", "full name"),
    "email": ("your email", "email", "email address"),
    "icp": ("what types of dental practices", "types of dental", "icp"),
    "geo": ("geographic target", "geography", "geo"),
    "services": ("what services do you sell", "services"),
    "calendly": ("your calendly", "calendly"),
    "workspace_email": ("google workspace email", "workspace email", "sending"),
    "deal_size": ("average deal size", "deal size"),
    "pricing": ("preferred pricing", "pricing model"),
    "extra": ("anything else", "other notes", "comments"),
}


def _env_url(key: str, default: str) -> str:
    return os.environ.get(key, "").strip() or default


def presentation_url() -> str:
    return _env_url("PRESENTATION_URL", DEFAULT_PRESENTATION)


def brief_form_url() -> str:
    return _env_url("BRIEF_FORM_URL", DEFAULT_BRIEF_FORM)


def calendly_url() -> str:
    return _env_url("CALENDLY_URL", DEFAULT_CALENDLY)


def send_async_onboarding(lead_email: str, first_name: str, company: str) -> bool:
    """Email presentation + onboarding form links to an interested lead."""
    name = (first_name or "").strip() or "there"
    email = (lead_email or "").strip()
    if not is_email(email):
        print(f"send_async_onboarding: bad email {email!r}")
        return False

    subject = "next steps — async onboarding"
    form = brief_form_url()
    preso = presentation_url()
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Great to hear you're interested in DentalMeet!\n"
        f"\n"
        f"Since we work 100% async (no meetings needed), here's how to get started:\n"
        f"\n"
        f"STEP 1 — 2-minute presentation (how it works + pricing):\n"
        f"{preso}\n"
        f"\n"
        f"STEP 2 — onboarding form (5 minutes) — fill this so I can launch:\n"
        f"{form}\n"
        f"\n"
        f"You don't need to hunt for the form inside the deck — the link is right here "
        f"in this email. Once I receive it, I'll launch your campaign within 24 hours.\n"
        f"\n"
        f"You'll get weekly reports every Monday with full transparency:\n"
        f"- Emails sent\n"
        f"- Open rates\n"
        f"- Replies received\n"
        f"- Meetings booked\n"
        f"\n"
        f"Any questions? Just reply to this email — I respond within a few hours.\n"
        f"\n"
        f"Looking forward to working together!\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
        f"hello@dentalmeet.agency\n"
        f"dentalmeet.agency\n"
        f"\n"
        f"P.S. Prefer a quick call instead? Book here: {calendly_url()}\n"
        f"\n"
        f"Form again (bookmark this): {form}\n"
    )
    ok = send_email(email, subject, body)
    if ok:
        print(f"async onboarding sent → {email} ({company or 'n/a'})")
    return ok


def _normalize_header(h: str) -> str:
    return " ".join((h or "").strip().lower().split())


def _header_map(headers: list[str]) -> dict[str, int]:
    """Map logical field → column index (0-based)."""
    norm = {_normalize_header(h): i for i, h in enumerate(headers)}
    out: dict[str, int] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            # exact or startswith match
            if alias in norm:
                out[field] = norm[alias]
                break
            for h, idx in norm.items():
                if h.startswith(alias) or alias in h:
                    out[field] = idx
                    break
            if field in out:
                break
    return out


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _get_or_create_clients_ws():
    import gspread

    gc = connect()
    sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
    sh = gc.open(sheet_name)
    try:
        ws = sh.worksheet("Clients")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title="Clients", rows=1000, cols=len(CLIENTS_HEADERS)
        )
        ws.update(
            range_name=_clients_range(1),
            values=[CLIENTS_HEADERS],
            value_input_option="USER_ENTERED",
        )
        return ws

    values = ws.get_all_values()
    if not values:
        ws.update(
            range_name=_clients_range(1),
            values=[CLIENTS_HEADERS],
            value_input_option="USER_ENTERED",
        )
    return ws


def _form_responses_ws():
    """Open the Google Form responses spreadsheet/worksheet."""
    gc = connect()
    sheet_name = os.environ.get(
        "FORM_RESPONSES_SPREADSHEET",
        "DentalMeet Pilot Onboarding (Responses)",
    ).strip()
    ws_name = os.environ.get("FORM_RESPONSES_WORKSHEET", "Form Responses 1").strip()
    try:
        sh = gc.open(sheet_name)
    except Exception as e:
        # Fallback: look inside Pilot CRM for a responses tab
        pilot = os.environ.get("SHEET_NAME", "Pilot CRM")
        try:
            sh = gc.open(pilot)
            ws = sh.worksheet(ws_name)
            return ws
        except Exception:
            raise RuntimeError(
                f"Cannot open form responses spreadsheet {sheet_name!r}: {e}. "
                f"Share it with the service account, or set FORM_RESPONSES_SPREADSHEET."
            ) from e
    try:
        return sh.worksheet(ws_name)
    except Exception:
        # First worksheet is usually the responses
        return sh.get_worksheet(0)


def _ensure_processed_col(ws, headers: list[str]) -> int:
    """Return 0-based index of Processed column; create if missing."""
    for i, h in enumerate(headers):
        if _normalize_header(h) == "processed":
            return i
    col = len(headers) + 1  # 1-based for gspread
    ws.update_cell(1, col, "Processed")
    return col - 1


def _append_client(ws, data: dict) -> None:
    values = ws.get_all_values()
    next_row = len(values) + 1
    # Dedup by email in column C
    email = data["Email"].lower()
    for i, row in enumerate(values[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email:
            print(f"client already exists: {email} (row {i})")
            return
    record = [data.get(h, "") for h in CLIENTS_HEADERS]
    # Pad payment cols empty for new form rows
    while len(record) < len(CLIENTS_HEADERS):
        record.append("")
    ws.update(
        range_name=_clients_range(next_row),
        values=[record],
        value_input_option="USER_ENTERED",
    )
    print(f"client added: {email}")


def _confirmation_email(name: str, email: str) -> bool:
    """Ack the form; payment instructions are sent right after separately."""
    first = (name or "").strip().split()[0] if (name or "").strip() else "there"
    subject = "got your form — next: payment"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Thanks for filling out the form — you're in.\n"
        f"\n"
        f"Next step is payment for the pilot. I'm sending the USDT (TRC20) "
        f"details in the next email.\n"
        f"\n"
        f"As soon as payment is confirmed, I'll connect your Workspace and "
        f"launch outreach within 24 hours.\n"
        f"\n"
        f"— Vladislav\n"
        f"DentalMeet\n"
    )
    return send_email(email, subject, body)


def check_form_responses() -> dict:
    """Ingest new Google Form rows into Clients + send confirmation."""
    print("=== check form responses ===")
    form_ws = _form_responses_ws()
    values = form_ws.get_all_values()
    if not values:
        print("form sheet empty")
        return {"processed": 0, "skipped": 0, "errors": 0}

    headers = values[0]
    fmap = _header_map(headers)
    processed_idx = _ensure_processed_col(form_ws, headers)
    # Re-read headers if we just added Processed
    if processed_idx >= len(headers):
        headers = form_ws.row_values(1)
        processed_idx = _ensure_processed_col(form_ws, headers)

    clients_ws = _get_or_create_clients_ws()
    today = date.today().isoformat()
    stats = {"processed": 0, "skipped": 0, "errors": 0}

    for i, raw in enumerate(values[1:], start=2):
        row = list(raw) + [""] * (max(processed_idx + 1, len(headers)) - len(raw))
        status = _cell(row, processed_idx).lower()
        if status in ("processed", "yes", "done"):
            stats["skipped"] += 1
            continue

        email = _cell(row, fmap.get("email"))
        if not is_email(email):
            # Try workspace email as fallback for identity
            email = _cell(row, fmap.get("workspace_email"))
        if not is_email(email):
            print(f"row {i}: no valid email, skip")
            stats["skipped"] += 1
            continue

        name = _cell(row, fmap.get("name"))
        company = _cell(row, fmap.get("company"))
        data = {
            "Company": company,
            "Name": name,
            "Email": email,
            "ICP": _cell(row, fmap.get("icp")),
            "Geo": _cell(row, fmap.get("geo")),
            "Services": _cell(row, fmap.get("services")),
            "Calendly": _cell(row, fmap.get("calendly")),
            "Workspace Email": _cell(row, fmap.get("workspace_email")),
            "Deal Size": _cell(row, fmap.get("deal_size")),
            "Pricing": _cell(row, fmap.get("pricing")),
            "Notes": _cell(row, fmap.get("extra")),
            "Status": "awaiting_payment",
            "Created": today,
        }

        try:
            from .ai_reply_assistant import send_payment_instructions

            _append_client(clients_ws, data)
            form_ws.update_cell(i, processed_idx + 1, "Processed")
            _confirmation_email(name, email)
            # Payment opens HERE — after brief is received, before delivery launch
            first = (name or "").split()[0] if name else ""
            send_payment_instructions(email, first)
            stats["processed"] += 1
            print(f"row {i}: processed {email} (+ payment instructions)")
        except Exception as e:
            stats["errors"] += 1
            print(f"row {i}: error {e}")

    print(
        f"summary: processed={stats['processed']} "
        f"skipped={stats['skipped']} errors={stats['errors']}"
    )
    print("=== end check forms ===")
    return stats
