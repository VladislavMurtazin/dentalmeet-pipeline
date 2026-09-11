"""Async onboarding: presentation + form after interest, then ingest form replies."""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from .email_sender import send_email
from .templates import signature_for
from .to_sheets import (
    get_all_values_safe,
    is_email,
    open_sheet_safe,
    open_worksheet_safe,
    retry_sheets_call,
    row_values_safe,
    sheets_update_safe,
    update_cell_safe,
)

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
    "Calendly API Token",
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
    "Client Type",
    "Engagement Number",
    "Payment Model",
    "Upfront Paid",
    "Pilot Completed",
    "Total Billed",
    "Total Paid",
    "Total Lifetime Value",
    "Number of Engagements",
    "Average Meetings per Engagement",
    "Meetings",
    "Completed Date",
]

QUALIFIED_MEETING_BLURB = (
    "What counts as a \"qualified\" meeting (you only pay for these):\n"
    "1. Meeting took place (not a no-show or cancellation)\n"
    "2. Duration >= 15 minutes\n"
    "3. Attendee is a decision-maker (Owner, CEO, Founder, President, "
    "Practice Manager, or Director)\n"
    "4. From your target geography (as in your brief)\n"
    "5. Represents a real dental practice (not a competitor / student / researcher)\n"
    "6. Not a duplicate of the same attendee in the same calendar month\n"
)


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
    "calendly": ("your calendly", "calendly link", "calendly url"),
    "calendly_token": (
        "calendly api token",
        "your calendly api token",
        "calendly token",
    ),
    "workspace_email": ("google workspace email", "workspace email", "sending"),
    "send_mode": (
        "how would you like to send emails",
        "email sending method",
        "send emails to dental practices",
        "workspace or plan b",
    ),
    "workspace_understanding": (
        "i understand that dentalmeet will only access",
        "understand email access",
        "workspace understanding",
        "email sending and reply tracking",
    ),
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


def _is_plan_b_send_mode(send_mode: str) -> bool:
    """True when client chose DentalMeet accounts instead of Workspace OAuth."""
    text = (send_mode or "").strip().lower()
    if not text:
        return False
    if "plan b" in text:
        return True
    if "dentalmeet" in text and ("behalf" in text or "no access" in text):
        return True
    if "no access" in text or "without connecting" in text:
        return True
    if "connect" in text and "workspace" in text:
        return False
    if "google workspace" in text and "recommended" in text:
        return False
    return "dentalmeet" in text or "our account" in text


def send_async_onboarding(
    lead_email: str,
    first_name: str,
    company: str,
    *,
    from_email: str | None = None,
) -> bool:
    """Email presentation + onboarding form links to an interested lead."""
    from .conversation_owner import resolve_from_email
    from .templates import signature_for

    name = (first_name or "").strip() or "there"
    email = (lead_email or "").strip()
    if not is_email(email):
        print(f"send_async_onboarding: bad email {email!r}")
        return False

    sender = (from_email or "").strip().lower() or resolve_from_email(email)
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
        f"PILOT PRICING (first engagement):\n"
        f"→ $0 upfront — you pay nothing to start\n"
        f"→ $150 per qualified meeting (max $750)\n"
        f"→ If we don't deliver 5 qualified meetings, you pay only for what we booked "
        f"(and $0 if none)\n"
        f"→ Payment is due AFTER the pilot — post-payment\n"
        f"\n"
        f"{QUALIFIED_MEETING_BLURB}"
        f"\n"
        f"Once I receive your form, I'll launch within 24 hours (no payment needed first).\n"
        f"\n"
        f"You'll get weekly reports every Monday with full transparency.\n"
        f"\n"
        f"Any questions? Just reply — I respond within a few hours.\n"
        f"\n"
        f"{signature_for(sender)}"
        f"\n"
        f"Form again (bookmark this): {form}\n"
    )
    ok = send_email(email, subject, body, from_email=sender)
    if ok:
        print(f"async onboarding sent → {email} ({company or 'n/a'}) via {sender or 'rotation'}")
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

    sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
    sh = open_sheet_safe(sheet_name)
    try:
        ws = open_worksheet_safe(sh, "Clients")
    except gspread.WorksheetNotFound:
        ws = retry_sheets_call(
            sh.add_worksheet, title="Clients", rows=1000, cols=len(CLIENTS_HEADERS)
        )
        sheets_update_safe(
            ws,
            range_name=_clients_range(1),
            values=[CLIENTS_HEADERS],
            value_input_option="USER_ENTERED",
        )
        return ws

    values = get_all_values_safe(ws)
    if not values:
        sheets_update_safe(
            ws,
            range_name=_clients_range(1),
            values=[CLIENTS_HEADERS],
            value_input_option="USER_ENTERED",
        )
    return ws


def _form_responses_ws():
    """Open the Google Form responses spreadsheet/worksheet."""
    sheet_name = os.environ.get(
        "FORM_RESPONSES_SPREADSHEET",
        "DentalMeet Pilot Onboarding (Responses)",
    ).strip()
    ws_name = os.environ.get("FORM_RESPONSES_WORKSHEET", "Form Responses 1").strip()
    try:
        sh = open_sheet_safe(sheet_name)
    except Exception as e:
        # Fallback: look inside Pilot CRM for a responses tab
        pilot = os.environ.get("SHEET_NAME", "Pilot CRM")
        try:
            sh = open_sheet_safe(pilot)
            ws = open_worksheet_safe(sh, ws_name)
            return ws
        except Exception:
            raise RuntimeError(
                f"Cannot open form responses spreadsheet {sheet_name!r}: {e}. "
                f"Share it with the service account, or set FORM_RESPONSES_SPREADSHEET."
            ) from e
    try:
        return open_worksheet_safe(sh, ws_name)
    except Exception:
        # First worksheet is usually the responses
        return retry_sheets_call(sh.get_worksheet, 0)


def _ensure_processed_col(ws, headers: list[str]) -> int:
    """Return 0-based index of Processed column; create if missing."""
    for i, h in enumerate(headers):
        if _normalize_header(h) == "processed":
            return i
    col = len(headers) + 1  # 1-based for gspread
    update_cell_safe(ws, 1, col, "Processed")
    return col - 1


def _pilot_welcome_email(
    name: str, email: str, *, plan_b: bool = False, from_email: str | None = None
) -> bool:
    first = (name or "").strip().split()[0] if (name or "").strip() else "there"
    subject = "welcome — pilot started (no upfront payment)"
    if plan_b:
        next_step = (
            "Next: we'll send from our accounts on your behalf (Plan B) — "
            "watch for a confirmation email. No Google Workspace connection needed."
        )
    else:
        next_step = (
            "Next: I'll connect your Workspace and launch outreach within 24 hours. "
            "Watch for a separate email explaining exactly what access we need."
        )
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Welcome! No upfront payment needed.\n"
        f"\n"
        f"We'll bill you after the pilot for qualified meetings only "
        f"($150 each, max $750). If we don't deliver, you pay nothing.\n"
        f"\n"
        f"{QUALIFIED_MEETING_BLURB}"
        f"\n"
        f"{next_step}\n"
        f"\n"
        f"Agreement summary (full terms on request):\n"
        f"— Pilot = post-payment\n"
        f"— Only qualified meetings are billed\n"
        f"— Full breakdown report with every invoice\n"
        f"\n"
        f"{signature_for(from_email)}"
    )
    return send_email(email, subject, body, from_email=from_email)


def _repeat_welcome_email(
    name: str, email: str, *, from_email: str | None = None
) -> bool:
    from .ai_reply_assistant import send_upfront_payment_instructions

    first = (name or "").strip().split()[0] if (name or "").strip() else "there"
    subject = "welcome back — $300 setup to relaunch"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Welcome back! Please pay $300 upfront to start this engagement.\n"
        f"\n"
        f"Then: $150 per qualified meeting "
        f"(or reply if you prefer the $1,000 flat for 7 meetings).\n"
        f"\n"
        f"{QUALIFIED_MEETING_BLURB}"
        f"\n"
        f"Payment details are in the next email (USDT TRC20).\n"
        f"\n"
        f"{signature_for(from_email)}"
    )
    ok = send_email(email, subject, body, from_email=from_email)
    send_upfront_payment_instructions(email, first, amount=300, from_email=from_email)
    return ok


def _continuation_email(name: str, email: str, *, from_email: str | None = None) -> bool:
    first = (name or "").strip().split()[0] if (name or "").strip() else "there"
    subject = "got your update — continuing your active pilot"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Thanks — you're already on an active engagement, so I won't create "
        f"a new project. We'll keep running on your current pilot terms "
        f"($0 upfront, $150/qualified meeting, max $750).\n"
        f"\n"
        f"If anything in the brief changed, just reply with the updates.\n"
        f"\n"
        f"{signature_for(from_email)}"
    )
    return send_email(email, subject, body, from_email=from_email)


def check_form_responses() -> dict:
    """Ingest new Google Form rows into Clients + validate + auto-launch delivery."""
    print("=== check form responses ===")
    form_ws = _form_responses_ws()
    values = get_all_values_safe(form_ws)
    if not values:
        print("form sheet empty")
        return {"processed": 0, "skipped": 0, "errors": 0, "invalid": 0, "launched": 0}

    headers = values[0]
    fmap = _header_map(headers)
    processed_idx = _ensure_processed_col(form_ws, headers)
    # Re-read headers if we just added Processed
    if processed_idx >= len(headers):
        headers = row_values_safe(form_ws, 1)
        processed_idx = _ensure_processed_col(form_ws, headers)

    clients_ws = _get_or_create_clients_ws()
    today = date.today().isoformat()
    stats = {
        "processed": 0,
        "skipped": 0,
        "errors": 0,
        "invalid": 0,
        "launched": 0,
    }

    for i, raw in enumerate(values[1:], start=2):
        row = list(raw) + [""] * (max(processed_idx + 1, len(headers)) - len(raw))
        status = _cell(row, processed_idx).lower()
        if status in ("processed", "yes", "done", "invalid"):
            stats["skipped"] += 1
            continue
        if status.startswith("invalid"):
            stats["skipped"] += 1
            continue

        email = _cell(row, fmap.get("email"))
        if not is_email(email):
            email = _cell(row, fmap.get("workspace_email"))
        if not is_email(email):
            print(f"row {i}: no valid email, skip")
            stats["skipped"] += 1
            continue

        name = _cell(row, fmap.get("name"))
        company = _cell(row, fmap.get("company"))
        send_mode = _cell(row, fmap.get("send_mode"))
        workspace_understanding = _cell(row, fmap.get("workspace_understanding"))
        plan_b = _is_plan_b_send_mode(send_mode)
        brief = {
            "Company": company,
            "Name": name,
            "Email": email,
            "ICP": _cell(row, fmap.get("icp")),
            "Geo": _cell(row, fmap.get("geo")),
            "Services": _cell(row, fmap.get("services")),
            "Calendly": _cell(row, fmap.get("calendly")),
            "Calendly API Token": _cell(row, fmap.get("calendly_token")),
            "Workspace Email": _cell(row, fmap.get("workspace_email")),
            "Deal Size": _cell(row, fmap.get("deal_size")),
            "Pricing": _cell(row, fmap.get("pricing")),
            "Notes": _cell(row, fmap.get("extra")),
            "Created": today,
            "Send Mode": send_mode,
            "Workspace Understanding": workspace_understanding,
            "workspace_send_mode": "fallback" if plan_b else "workspace",
        }

        # --- validate before creating client ---
        from delivery.launch_client import (
            launch_client,
            send_form_fix_email,
            validate_brief,
        )

        check = validate_brief(brief)
        if not check["ok"]:
            print(f"row {i}: INVALID {email}: {check['errors']}")
            try:
                from .conversation_owner import resolve_post_form_from_email

                owner = resolve_post_form_from_email(email)
                if not owner:
                    print(
                        f"row {i}: ERROR no Google Workspace SMTP password — "
                        f"skipping fix email for {email} (will not use Zoho)"
                    )
                else:
                    send_form_fix_email(
                        email,
                        name,
                        check["errors"],
                        from_email=owner,
                    )
            except Exception as e:
                print(f"row {i}: fix email failed: {e}")
            update_cell_safe(
                form_ws,
                i,
                processed_idx + 1,
                f"Invalid: {'; '.join(check['errors'])[:180]}",
            )
            stats["invalid"] += 1
            continue

        for w in check.get("warnings") or []:
            print(f"row {i}: warning: {w}")

        try:
            from delivery.client_tracker import create_client_record

            _ = clients_ws
            record = create_client_record(email, company, brief)
            update_cell_safe(form_ws, i, processed_idx + 1, "Processed")

            from .conversation_owner import resolve_post_form_from_email

            owner = resolve_post_form_from_email(email)
            if not owner:
                print(
                    f"row {i}: ERROR no Google Workspace SMTP password — "
                    f"skipping post-form client emails for {email} "
                    f"(will not use Zoho)"
                )
            else:
                print(f"row {i}: thread owner {owner}")

            if owner and record.get("continuation"):
                _continuation_email(name, email, from_email=owner)
                label = "continuation"
            elif owner and record.get("is_repeat"):
                _repeat_welcome_email(name, email, from_email=owner)
                label = "repeat + $300 upfront"
            elif owner:
                _pilot_welcome_email(name, email, plan_b=plan_b, from_email=owner)
                label = "pilot $0 upfront"
            else:
                if record.get("continuation"):
                    label = "continuation (email skipped)"
                elif record.get("is_repeat"):
                    label = "repeat (email skipped)"
                else:
                    label = "pilot (email skipped)"

            if owner and plan_b and not record.get("continuation"):
                from delivery.connect_workspace import set_workspace_status
                from delivery.workspace_transparency import send_plan_b_confirmation

                set_workspace_status(email, "fallback")
                send_plan_b_confirmation(
                    email, name=name, company=company, from_email=owner
                )
                print(f"row {i}: Plan B selected — skipped OAuth, sent confirmation")
            elif plan_b and not record.get("continuation"):
                from delivery.connect_workspace import set_workspace_status

                set_workspace_status(email, "fallback")
                print(
                    f"row {i}: Plan B selected — skipped OAuth; "
                    f"confirmation email skipped (no Google SMTP)"
                )

            stats["processed"] += 1
            print(f"row {i}: processed {email} ({label})")

            # Auto-launch dental research (+ send if Workspace already connected).
            # Repeat clients still awaiting upfront payment are skipped.
            should_launch = True
            if record.get("continuation"):
                should_launch = False
            if record.get("is_repeat") and record.get("status") == "awaiting_upfront":
                should_launch = False
                print(f"row {i}: skip launch until upfront paid")

            if should_launch:
                try:
                    launch_result = launch_client(
                        email,
                        name=name,
                        send_connect_link=not plan_b,
                    )
                    if launch_result.get("status") in ("running", "awaiting_workspace"):
                        stats["launched"] += 1
                    print(
                        f"row {i}: launch status={launch_result.get('status')} "
                        f"targets={launch_result.get('targets')} "
                        f"sent={launch_result.get('sent')}"
                    )
                except Exception as e:
                    print(f"row {i}: launch error (client still saved): {e}")

        except Exception as e:
            stats["errors"] += 1
            print(f"row {i}: error {e}")

    print(
        f"summary: processed={stats['processed']} invalid={stats['invalid']} "
        f"launched={stats['launched']} skipped={stats['skipped']} "
        f"errors={stats['errors']}"
    )
    print("=== end check forms ===")
    return stats
