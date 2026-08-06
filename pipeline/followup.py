"""Automatic follow-up waves against Pilot CRM / Agencies."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from .email_sender import send_batch
from .templates import CALENDLY_URL, FOLLOWUP_1_TEMPLATE, FOLLOWUP_2_TEMPLATE
from .to_sheets import HEADERS, get_worksheet, is_email

COL = {name: i + 1 for i, name in enumerate(HEADERS)}
NOTES_COL = COL["Notes"]

MAX_FOLLOWUPS_PER_RUN = 10
FU1_DAYS = 3
FU2_DAYS = 7

# Notes markers, e.g. "; FU1 sent 2026-08-06"
FU_MARKER_RE = re.compile(r"\bFU([12])\s+sent\b", re.I)


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _pad(row: list[str], n: int = 12) -> list[str]:
    return (list(row) + [""] * n)[:n]


def _first_name(contact: str) -> str:
    contact = (contact or "").strip()
    return contact.split()[0] if contact else ""


def _parse_sent_date(value: str) -> date | None:
    """Parse Sent cell safely; return None if empty/malformed."""
    raw = (value or "").strip()
    if not raw:
        return None
    # Accept YYYY-MM-DD or datetime-ish prefixes
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10] if fmt.startswith("%Y") else raw, fmt).date()
        except ValueError:
            continue
    # Last resort: first 10 chars as ISO
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _fu_already_sent(notes: str, followup_number: int) -> bool:
    notes = notes or ""
    for m in FU_MARKER_RE.finditer(notes):
        if int(m.group(1)) == followup_number:
            return True
    return False


def get_followup_candidates(days_since_sent: int, followup_number: int) -> list[dict]:
    """Leads whose Sent date is >= days_since_sent ago and Replied is empty."""
    ws = get_worksheet("Agencies")
    values = ws.get_all_values()
    today = date.today()
    cutoff = today - timedelta(days=int(days_since_sent))
    candidates: list[dict] = []

    for i, raw in enumerate(values):
        if i == 0:
            continue
        row_index = i + 1
        cells = _pad(raw)
        fields = {name: cells[j] for j, name in enumerate(HEADERS)}

        email = fields["Email"].strip()
        if not is_email(email):
            continue
        if fields["Replied"].strip():
            continue

        sent_on = _parse_sent_date(fields["Sent"])
        if sent_on is None:
            continue
        # Sent N+ days ago ⇒ sent_on <= cutoff
        if sent_on > cutoff:
            continue

        notes = fields["Notes"]
        # Don't re-send the same follow-up wave
        if _fu_already_sent(notes, followup_number):
            continue
        # FU2 requires FU1 already logged (and no reply — checked above)
        if followup_number == 2 and not _fu_already_sent(notes, 1):
            continue

        candidates.append(
            {
                "row_index": row_index,
                "Email": email,
                "Contact": fields["Contact"].strip(),
                "Company": fields["Company"].strip(),
                "Notes": notes,
                "followup_number": followup_number,
            }
        )

    return candidates


def _append_notes(row_index: int, current_notes: str, marker: str) -> None:
    """Append follow-up marker to Notes column."""
    notes = (current_notes or "").rstrip()
    if marker.strip() in notes:
        return
    new_notes = f"{notes}{marker}" if notes else marker.lstrip("; ").strip()
    ws = get_worksheet("Agencies")
    ws.update_cell(row_index, NOTES_COL, new_notes)


def run_followups(followup_number: int) -> dict:
    """Send follow-up wave 1 (3+ days) or 2 (7+ days). Max 10 per run."""
    followup_number = int(followup_number)
    if followup_number not in (1, 2):
        raise ValueError("followup_number must be 1 or 2")

    days = FU1_DAYS if followup_number == 1 else FU2_DAYS
    template = FOLLOWUP_1_TEMPLATE if followup_number == 1 else FOLLOWUP_2_TEMPLATE
    tag = f"FU{followup_number}"

    candidates = get_followup_candidates(days, followup_number)
    batch = candidates[:MAX_FOLLOWUPS_PER_RUN]
    print(
        f"followup {followup_number}: {len(candidates)} candidates "
        f"(sent {days}+ days ago, no reply); sending up to {len(batch)}"
    )

    if not batch:
        print("nothing to follow up")
        return {"sent": [], "failed": []}

    recipients: list[dict] = []
    for lead in batch:
        subject, body = template(
            first_name=_first_name(lead.get("Contact", "")),
            company=lead.get("Company", ""),
            calendly_url=CALENDLY_URL,
        )
        recipients.append(
            {
                "to": lead["Email"],
                "subject": subject,
                "body": body,
                "row_index": lead["row_index"],
                "Notes": lead.get("Notes", ""),
                "followup_number": followup_number,
            }
        )

    result = send_batch(
        recipients,
        delay_min=60,
        delay_max=60,
        max_per_run=MAX_FOLLOWUPS_PER_RUN,
    )

    today = date.today().isoformat()
    marker = f"; {tag} sent {today}"

    if _dry_run():
        print(
            f"DRY_RUN: not updating Notes "
            f"({len(result['sent'])} would get '{marker}')"
        )
    else:
        for item in result["sent"]:
            row_index = item.get("row_index")
            if not row_index:
                continue
            try:
                _append_notes(row_index, item.get("Notes", ""), marker)
                print(f"Notes += '{marker}' row {row_index}")
            except Exception as e:
                print(f"Notes update failed row {row_index}: {e}")

    print(
        f"summary FU{followup_number}: "
        f"sent {len(result['sent'])}, failed {len(result['failed'])}"
    )
    return result
