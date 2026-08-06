"""First-touch email campaign against Pilot CRM / Agencies."""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from .email_sender import MAX_PER_RUN, send_batch
from .templates import CALENDLY_URL, FIRST_EMAIL_TEMPLATE
from .to_sheets import HEADERS, get_worksheet, is_email

# Column letters / 1-based indexes matching HEADERS
COL = {name: i + 1 for i, name in enumerate(HEADERS)}
SENT_COL = COL["Sent"]  # G


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _pad(row: list[str], n: int = 12) -> list[str]:
    out = list(row) + [""] * n
    return out[:n]


def _first_name(contact: str) -> str:
    contact = (contact or "").strip()
    if not contact:
        return ""
    return contact.split()[0]


def get_unsent_leads() -> list[dict]:
    """Leads with valid email, empty Sent, Verified in {yes, manual}."""
    ws = get_worksheet("Agencies")
    values = ws.get_all_values()
    leads: list[dict] = []

    for i, raw in enumerate(values):
        if i == 0:
            continue  # header
        row_index = i + 1  # 1-based sheet row
        cells = _pad(raw)
        fields = {name: cells[j] for j, name in enumerate(HEADERS)}

        email = fields["Email"].strip()
        verified = fields["Verified"].strip().lower()
        sent = fields["Sent"].strip()

        if not is_email(email):
            continue
        if sent:
            continue
        if verified not in ("yes", "manual"):
            continue

        leads.append({"row_index": row_index, **fields})

    return leads


def mark_sent(row_index: int, date_str: str) -> None:
    """Write YYYY-MM-DD into the Sent column for a sheet row."""
    ws = get_worksheet("Agencies")
    ws.update_cell(row_index, SENT_COL, date_str)


def run_first_touch(limit: int = 5) -> dict:
    """Build and send first-touch emails for up to `limit` unsent leads."""
    limit = max(1, min(int(limit), MAX_PER_RUN))
    leads = get_unsent_leads()
    batch_leads = leads[:limit]
    print(f"unsent leads: {len(leads)}; sending up to {limit}")

    if not batch_leads:
        print("nothing to send")
        return {"sent": [], "failed": []}

    recipients: list[dict] = []
    for lead in batch_leads:
        company = lead.get("Company", "").strip()
        opener = lead.get("Signal", "").strip()
        if not opener:
            opener = (
                f"Saw {company or 'your agency'} works with dental practices — "
                f"that's exactly the niche my outbound engine specializes in."
            )
        first = _first_name(lead.get("Contact", ""))
        subject, body = FIRST_EMAIL_TEMPLATE(
            first_name=first,
            company=company,
            opener=opener,
            calendly_url=CALENDLY_URL,
        )
        recipients.append(
            {
                "to": lead["Email"].strip(),
                "subject": subject,
                "body": body,
                "row_index": lead["row_index"],
            }
        )

    result = send_batch(recipients)
    today = date.today().isoformat()

    if _dry_run():
        print(f"DRY_RUN: not marking Sent ({len(result['sent'])} would be marked {today})")
    else:
        for item in result["sent"]:
            row_index = item.get("row_index")
            if row_index:
                try:
                    mark_sent(row_index, today)
                    print(f"marked Sent={today} row {row_index}")
                except Exception as e:
                    print(f"mark_sent failed row {row_index}: {e}")

    print(f"summary: sent {len(result['sent'])}, failed {len(result['failed'])}")
    return result
