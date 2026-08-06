"""Pilot vs Repeat client tracking and payment-model assignment."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

PILOT_MODEL_LABEL = "Pilot ($0 upfront, $150/meeting, max $750)"
REPEAT_MODEL_LABEL = "Repeat ($300 upfront + $150/meeting)"
REPEAT_FLAT_LABEL = "Repeat ($1000 flat for 7 meetings)"

PILOT_PAYMENT = {
    "upfront": 0,
    "per_meeting": 150,
    "max": 750,
    "label": PILOT_MODEL_LABEL,
    "client_type": "Pilot",
}
REPEAT_PAYMENT = {
    "upfront": 300,
    "per_meeting": 150,
    "max": None,
    "label": REPEAT_MODEL_LABEL,
    "client_type": "Repeat",
}
REPEAT_FLAT_PAYMENT = {
    "upfront": 1000,
    "per_meeting": 0,
    "max": 1000,
    "flat_meetings": 7,
    "label": REPEAT_FLAT_LABEL,
    "client_type": "Repeat",
}

# Extra columns beyond the original onboarding headers
TRACKER_HEADERS = [
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
    "Calendly API Token",
    "Payment Status",
    "Payment TXID",
    "Payment Date",
    "Payment Amount",
]


def _clients_ws():
    from pipeline.to_sheets import connect
    import gspread

    gc = connect()
    sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
    try:
        return sh.worksheet("Clients")
    except gspread.WorksheetNotFound as e:
        raise RuntimeError("Clients worksheet missing — run check_forms first") from e


def _headers(ws) -> list[str]:
    return [h.strip() for h in (ws.row_values(1) or [])]


def ensure_tracker_columns(ws=None) -> dict[str, int]:
    """Ensure tracker columns exist; return header → 1-based col index."""
    ws = ws or _clients_ws()
    headers = _headers(ws)
    changed = False
    for name in TRACKER_HEADERS:
        if name not in headers:
            headers.append(name)
            changed = True
    if changed:
        ws.update(
            range_name=f"A1:{_col_letter(len(headers))}1",
            values=[headers],
            value_input_option="USER_ENTERED",
        )
    return {h: i + 1 for i, h in enumerate(headers)}


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _get(row: list[str], cols: dict[str, int], name: str, default: str = "") -> str:
    i = cols.get(name)
    if i is None or i - 1 >= len(row):
        return default
    return (row[i - 1] or "").strip()


def _find_matches(
    values: list[list[str]],
    cols: dict[str, int],
    client_email: str,
    company_name: str = "",
) -> list[tuple[int, list[str]]]:
    """Return (row_index, row) matches by email or company (newest last)."""
    email = (client_email or "").strip().lower()
    company = (company_name or "").strip().lower()
    email_i = cols.get("Email", 3)
    company_i = cols.get("Company", 1)
    hits: list[tuple[int, list[str]]] = []
    for i, row in enumerate(values[1:], start=2):
        row_email = (row[email_i - 1] if len(row) >= email_i else "").strip().lower()
        row_co = (row[company_i - 1] if len(row) >= company_i else "").strip().lower()
        if email and row_email == email:
            hits.append((i, row))
        elif company and row_co and row_co == company:
            hits.append((i, row))
    return hits


def check_if_repeat(client_email: str, company_name: str = "") -> dict:
    """
    Determine Pilot vs Repeat / continuation.

    Returns:
      is_repeat, engagement_number, status, row_index (if found), match
    """
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = ws.get_all_values()
    if not values:
        return {
            "is_repeat": False,
            "engagement_number": 1,
            "status": "",
            "row_index": None,
            "match": None,
        }

    hits = _find_matches(values, cols, client_email, company_name)
    if not hits:
        return {
            "is_repeat": False,
            "engagement_number": 1,
            "status": "",
            "row_index": None,
            "match": None,
        }

    row_index, row = hits[-1]  # most recent
    status = _get(row, cols, "Status").lower()
    try:
        eng = int(_get(row, cols, "Engagement Number") or "1")
    except ValueError:
        eng = 1

    if status in ("active", "onboarded", "awaiting_payment", "awaiting_upfront"):
        return {
            "is_repeat": False,
            "engagement_number": eng,
            "status": status,
            "row_index": row_index,
            "match": "continuation",
            "client_type": _get(row, cols, "Client Type") or "Pilot",
        }

    if status in ("completed", "churned", "done", "pilot_completed"):
        return {
            "is_repeat": True,
            "engagement_number": eng + 1,
            "status": status,
            "row_index": row_index,
            "match": "repeat",
            "prior_client_type": _get(row, cols, "Client Type") or "Pilot",
        }

    # Unknown status with existing row → treat as continuation
    return {
        "is_repeat": False,
        "engagement_number": eng,
        "status": status or "active",
        "row_index": row_index,
        "match": "continuation",
        "client_type": _get(row, cols, "Client Type") or "Pilot",
    }


def _pricing_prefers_flat(brief_data: dict) -> bool:
    raw = (
        (brief_data.get("Pricing") or "")
        + " "
        + (brief_data.get("pricing") or "")
        + " "
        + (brief_data.get("Notes") or "")
    ).lower()
    return "1000" in raw or "flat" in raw or "7 meeting" in raw


def create_client_record(
    client_email: str,
    company_name: str = "",
    brief_data: dict | None = None,
) -> dict:
    """
    Create or continue a Clients row with Pilot/Repeat payment model.
    """
    brief_data = dict(brief_data or {})
    email = (client_email or brief_data.get("Email") or "").strip()
    company = (company_name or brief_data.get("Company") or "").strip()
    check = check_if_repeat(email, company)

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    headers = _headers(ws)
    today = date.today().isoformat()

    # Continuation of active engagement — do not create a new row
    if check.get("match") == "continuation" and check.get("row_index"):
        print(
            f"continuation: {email} row {check['row_index']} "
            f"(engagement {check['engagement_number']})"
        )
        return {
            "created": False,
            "continuation": True,
            "is_repeat": False,
            "client_type": check.get("client_type") or "Pilot",
            "engagement_number": check["engagement_number"],
            "payment_model": get_client_payment_model(email),
            "row_index": check["row_index"],
            "email": email,
            "company": company,
            "name": brief_data.get("Name") or brief_data.get("name") or "",
        }

    is_repeat = bool(check.get("is_repeat"))
    engagement = int(check.get("engagement_number") or 1)

    if is_repeat:
        client_type = "Repeat"
        if _pricing_prefers_flat(brief_data):
            payment_label = REPEAT_FLAT_LABEL
            upfront_paid = "Pending"
            status = "awaiting_upfront"
        else:
            payment_label = REPEAT_MODEL_LABEL
            upfront_paid = "Pending"
            status = "awaiting_upfront"
    else:
        client_type = "Pilot"
        payment_label = PILOT_MODEL_LABEL
        upfront_paid = "No"
        status = "active"

    # Count prior engagements for LTV columns
    values = ws.get_all_values()
    prior_hits = _find_matches(values, cols, email, company)
    num_engagements = engagement

    record_map = {
        "Company": company,
        "Name": brief_data.get("Name") or brief_data.get("name") or "",
        "Email": email,
        "ICP": brief_data.get("ICP") or brief_data.get("icp") or "",
        "Geo": brief_data.get("Geo") or brief_data.get("geo") or "",
        "Services": brief_data.get("Services") or brief_data.get("services") or "",
        "Calendly": brief_data.get("Calendly") or brief_data.get("calendly") or "",
        "Calendly API Token": brief_data.get("Calendly API Token")
        or brief_data.get("calendly_token")
        or "",
        "Workspace Email": brief_data.get("Workspace Email")
        or brief_data.get("workspace_email")
        or "",
        "Deal Size": brief_data.get("Deal Size") or brief_data.get("deal_size") or "",
        "Pricing": brief_data.get("Pricing") or brief_data.get("pricing") or "",
        "Notes": brief_data.get("Notes") or brief_data.get("extra") or "",
        "Status": status,
        "Created": today,
        "Client Type": client_type,
        "Engagement Number": str(engagement),
        "Payment Model": payment_label,
        "Upfront Paid": upfront_paid,
        "Pilot Completed": "No",
        "Total Billed": "0",
        "Total Paid": "0",
        "Total Lifetime Value": _sum_ltv(prior_hits, cols),
        "Number of Engagements": str(num_engagements),
        "Average Meetings per Engagement": _avg_meetings(prior_hits, cols),
        "Meetings": "0",
        "Payment Status": "unpaid" if is_repeat else "post_pay",
    }

    # Build row aligned to current headers
    row_vals = [record_map.get(h, "") for h in headers]
    next_row = len(values) + 1
    ws.update(
        range_name=f"A{next_row}:{_col_letter(len(headers))}{next_row}",
        values=[row_vals],
        value_input_option="USER_ENTERED",
    )
    print(f"client created: {email} type={client_type} eng={engagement}")

    return {
        "created": True,
        "continuation": False,
        "is_repeat": is_repeat,
        "client_type": client_type,
        "engagement_number": engagement,
        "payment_model": get_client_payment_model(email),
        "payment_model_label": payment_label,
        "row_index": next_row,
        "email": email,
        "company": company,
        "name": record_map["Name"],
        "status": status,
    }


def _sum_ltv(hits: list[tuple[int, list[str]]], cols: dict[str, int]) -> str:
    total = 0.0
    for _, row in hits:
        for key in ("Total Lifetime Value", "Total Paid", "Payment Amount"):
            raw = _get(row, cols, key)
            if raw:
                try:
                    total = max(total, float(raw.replace(",", "").replace("$", "")))
                except ValueError:
                    pass
                break
    return str(int(total)) if total == int(total) else f"{total:.2f}"


def _avg_meetings(hits: list[tuple[int, list[str]]], cols: dict[str, int]) -> str:
    if not hits:
        return "0"
    nums = []
    for _, row in hits:
        raw = _get(row, cols, "Meetings") or "0"
        try:
            nums.append(float(raw))
        except ValueError:
            nums.append(0.0)
    if not nums:
        return "0"
    avg = sum(nums) / len(nums)
    return f"{avg:.1f}"


def get_client_payment_model(client_email: str) -> dict:
    """Return {upfront, per_meeting, max, label, client_type} for a client."""
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = ws.get_all_values()
    hits = _find_matches(values, cols, client_email, "")
    if not hits:
        return {**PILOT_PAYMENT, "client_type": "Pilot"}

    _, row = hits[-1]
    ctype = (_get(row, cols, "Client Type") or "Pilot").strip()
    model_label = (_get(row, cols, "Payment Model") or "").lower()
    pricing = (_get(row, cols, "Pricing") or "").lower()

    if ctype.lower() == "repeat" or "repeat" in model_label:
        if "1000" in model_label or "1000" in pricing or "flat" in model_label:
            return {**REPEAT_FLAT_PAYMENT, "client_type": "Repeat"}
        return {**REPEAT_PAYMENT, "client_type": "Repeat"}
    return {**PILOT_PAYMENT, "client_type": "Pilot"}


def update_client_after_pilot(client_email: str) -> dict:
    """Mark pilot completed, send invoice (stages 6–7), thank-you / continue note."""
    from .billing import send_invoice

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = ws.get_all_values()
    hits = _find_matches(values, cols, client_email, "")
    if not hits:
        raise RuntimeError(f"Client not found: {client_email}")

    row_index, row = hits[-1]
    prior_paid_raw = _get(row, cols, "Total Paid") or "0"
    try:
        prior_paid = float(prior_paid_raw.replace(",", "").replace("$", ""))
    except ValueError:
        prior_paid = 0.0

    prior_ltv_raw = _get(row, cols, "Total Lifetime Value") or "0"
    try:
        prior_ltv = float(prior_ltv_raw.replace(",", "").replace("$", ""))
    except ValueError:
        prior_ltv = 0.0

    # Stages 5–7: qualify + invoice PDF/HTML + email with TRC20
    inv = send_invoice(client_email, period="month")
    total_billed = float(inv.get("amount") or 0)
    qualified = int(inv.get("qualified_meetings") or 0)
    new_ltv = prior_ltv + total_billed
    today = date.today().isoformat()

    updates = {
        "Pilot Completed": "Yes",
        "Status": "Completed",
        "Completed Date": today,
        "Meetings": str(qualified),
        "Total Billed": str(int(total_billed)),
        "Total Lifetime Value": str(int(new_ltv)),
        "Number of Engagements": _get(row, cols, "Engagement Number") or "1",
        "Payment Status": "unpaid" if total_billed > 0 else "n/a",
    }
    # Re-read cols in case sheet grew
    cols = ensure_tracker_columns(ws)
    for key, val in updates.items():
        if key in cols:
            ws.update_cell(row_index, cols[key], val)

    print(f"pilot completed: {client_email} billed=${total_billed} meetings={qualified}")
    return {
        "email": client_email,
        "qualified_meetings": qualified,
        "total_billed": total_billed,
        "total_paid": prior_paid,
        "ltv": new_ltv,
        "status": "Completed",
        "invoice": {
            "number": inv.get("invoice_number"),
            "sent": inv.get("sent"),
            "pdf_path": inv.get("pdf_path"),
        },
    }


def send_pilot_followup_reminder(client_email: str) -> bool:
    """Reminder ~7 days after pilot completion."""
    from pipeline.email_sender import send_email

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = ws.get_all_values()
    hits = _find_matches(values, cols, client_email, "")
    if not hits:
        return False
    _, row = hits[-1]
    name = _get(row, cols, "Name")
    first = (name.split()[0] if name else "") or "there"
    subject = "ready to continue?"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Hope the pilot went well!\n"
        f"\n"
        f"If you'd like to continue, our repeat pricing is "
        f"$300 upfront + $150/qualified meeting "
        f"(or $1,000 flat for 7 meetings).\n"
        f"\n"
        f"Ready to start? Just reply here.\n"
        f"\n"
        f"— Vladislav\n"
        f"DentalMeet\n"
    )
    return send_email(client_email, subject, body)


def remind_completed_pilots(days: int = 7) -> dict:
    """Send continue reminders for pilots completed exactly ~N days ago."""
    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = ws.get_all_values()
    target = (date.today() - timedelta(days=days)).isoformat()
    sent = []
    for i, row in enumerate(values[1:], start=2):
        if _get(row, cols, "Status").lower() != "completed":
            continue
        if (_get(row, cols, "Client Type") or "").lower() != "pilot":
            continue
        completed = _get(row, cols, "Completed Date")
        if completed != target:
            continue
        notes = _get(row, cols, "Notes")
        if "pilot_continue_reminder_sent" in notes.lower():
            continue
        email = _get(row, cols, "Email")
        if send_pilot_followup_reminder(email):
            sent.append(email)
            if "Notes" in cols:
                marker = (notes + "; " if notes else "") + "pilot_continue_reminder_sent"
                ws.update_cell(i, cols["Notes"], marker)
    print(f"pilot continue reminders: {len(sent)}")
    return {"sent": sent}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pilot / Repeat client tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Check if client is repeat")
    p_check.add_argument("email")
    p_check.add_argument("--company", default="")

    p_create = sub.add_parser("create", help="Create client record from email")
    p_create.add_argument("email")
    p_create.add_argument("--company", default="")
    p_create.add_argument("--name", default="")

    p_done = sub.add_parser("complete_pilot", help="Mark pilot done + thank-you email")
    p_done.add_argument("email")

    p_rem = sub.add_parser("remind", help="Send D+7 continue reminders")
    p_rem.add_argument("--days", type=int, default=7)

    args = parser.parse_args(argv)

    if args.cmd == "check":
        result = check_if_repeat(args.email, args.company)
        print(result)
        return 0
    if args.cmd == "create":
        result = create_client_record(
            args.email,
            args.company,
            {"Name": args.name, "Email": args.email, "Company": args.company},
        )
        print(result)
        return 0
    if args.cmd == "complete_pilot":
        print(update_client_after_pilot(args.email))
        return 0
    if args.cmd == "remind":
        print(remind_completed_pilots(days=args.days))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
