"""2-month re-engage for Agencies cold first-touch (agency founders).

A lead that was first-touched, never replied, and whose Sent date is at least
``REENGAGE_DAYS`` (default 60) calendar days ago becomes eligible for another
first-touch. This is the Agencies sheet used by ``pipeline.main send`` /
``campaign.get_unsent_leads`` — not Plan B clinic CSV targets.

Window: 60 days (not calendar months). Override with ``REENGAGE_DAYS``.

Cap: Notes marker ``reengage_reset YYYY-MM-DD``. A lead is skipped if that
marker exists and is newer than ``REENGAGE_DAYS``. Combined with rewriting
Sent after a successful send, this cannot re-mail the same lead every day.

History: Sent is not wiped just to scan eligibility. On a successful re-engage
send (or ``reengage --apply``), the old Sent is appended to Notes as
``sent_was YYYY-MM-DD; reengage_reset YYYY-MM-DD``.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from .to_sheets import HEADERS, get_all_values_safe, get_worksheet_safe, is_email, update_cell_safe

COL = {name: i + 1 for i, name in enumerate(HEADERS)}
SENT_COL = COL["Sent"]
NOTES_COL = COL["Notes"]

DEFAULT_REENGAGE_DAYS = 60
REENGAGE_MARKER_RE = re.compile(r"\breengage_reset\s+(\d{4}-\d{2}-\d{2})\b", re.I)

# Notes tokens that mean "do not mail again" (bounce / opt-out / decline).
_BLOCK_NOTE_RE = re.compile(
    r"(?i)(?:"
    r"\bunsubscribed\b|"
    r"\bunsubscribe\b|"
    r"\bnot_interested\b|"
    r"\bdo_not_contact\b|"
    r"\bstatus=bounced\b|"
    r"\bbounced\b"
    r")"
)

# Reply type values that show an active thread or a hard stop.
BLOCK_REPLY_TYPES = frozenset(
    {
        "bounced",
        "bounce",
        "unsubscribe",
        "unsubscribed",
        "not_interested",
        "interested",
        "question",
        "meeting",
    }
)

# Meeting column: any of these (or any other non-empty value) = active thread.
BLOCK_MEETING_VALUES = frozenset(
    {
        "yes",
        "booked",
        "scheduled",
        "response_sent",
        "done",
        "held",
        "1",
        "true",
    }
)


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def reengage_stale_days() -> int:
    raw = os.environ.get("REENGAGE_DAYS", str(DEFAULT_REENGAGE_DAYS)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_REENGAGE_DAYS


def parse_sent_date(value: str) -> date | None:
    """Parse a Sent cell; return None if empty/malformed."""
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            sample = raw[:10] if fmt.startswith("%Y") else raw
            return datetime.strptime(sample, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def last_reengage_reset(notes: str) -> date | None:
    """Latest ``reengage_reset YYYY-MM-DD`` in Notes, or None."""
    found: date | None = None
    for match in REENGAGE_MARKER_RE.finditer(notes or ""):
        try:
            parsed = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if found is None or parsed > found:
            found = parsed
    return found


def notes_block_reengage(notes: str) -> bool:
    return bool(_BLOCK_NOTE_RE.search(notes or ""))


def _pad(row: list[str], n: int | None = None) -> list[str]:
    width = n if n is not None else len(HEADERS)
    return (list(row) + [""] * width)[:width]


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def reengage_ineligibility_reason(
    fields: dict,
    *,
    today: date | None = None,
    stale_days: int | None = None,
) -> str | None:
    """Why this Agencies row must not be re-engaged, or None if eligible.

    Pure sheet-field check — bounce files / suppression lists are applied by
    the caller (``get_unsent_leads`` / ``get_reengage_leads``).
    """
    today = today or date.today()
    days = reengage_stale_days() if stale_days is None else max(1, int(stale_days))
    cutoff = today - timedelta(days=days)

    email = (fields.get("Email") or "").strip()
    if not is_email(email):
        return "invalid_email"

    verified = _norm(fields.get("Verified") or "")
    if verified not in ("yes", "manual"):
        return "not_verified"

    if (fields.get("Replied") or "").strip():
        return "replied"

    meeting = (fields.get("Meeting") or "").strip()
    if meeting:
        return "meeting_active"

    reply_type = _norm(fields.get("Reply type") or "")
    if reply_type in BLOCK_REPLY_TYPES:
        return f"reply_type:{reply_type}"

    notes = fields.get("Notes") or ""
    if notes_block_reengage(notes):
        return "notes_block"

    sent_on = parse_sent_date(fields.get("Sent") or "")
    if sent_on is None:
        return "no_sent_date"
    if sent_on > cutoff:
        return "sent_too_recent"

    reset_on = last_reengage_reset(notes)
    if reset_on is not None and reset_on > cutoff:
        return "reengage_cap"

    return None


def is_reengage_eligible(
    fields: dict,
    *,
    today: date | None = None,
    stale_days: int | None = None,
) -> bool:
    """True when a silent Agencies lead should get another first-touch."""
    return (
        reengage_ineligibility_reason(fields, today=today, stale_days=stale_days)
        is None
    )


def reengage_notes_marker(old_sent: str, today: date | None = None) -> str:
    """Notes fragment that preserves Sent history and caps the cycle."""
    today = today or date.today()
    old = (old_sent or "").strip() or "unknown"
    return f"sent_was {old}; reengage_reset {today.isoformat()}"


def append_reengage_notes(notes: str, old_sent: str, today: date | None = None) -> str:
    marker = reengage_notes_marker(old_sent, today)
    current = (notes or "").rstrip()
    if marker in current:
        return current
    if current:
        return f"{current}; {marker}"
    return marker


def get_reengage_leads(*, today: date | None = None) -> list[dict]:
    """Agencies rows eligible for a 2-month silent re-engage."""
    from .bounce_handler import check_recipient_sendable

    today = today or date.today()
    ws = get_worksheet_safe("Agencies")
    values = get_all_values_safe(ws)
    leads: list[dict] = []

    try:
        from .ai_reply_assistant import is_suppressed
    except Exception:  # pragma: no cover - optional guard
        is_suppressed = None

    for i, raw in enumerate(values):
        if i == 0:
            continue
        row_index = i + 1
        cells = _pad(raw)
        fields = {name: cells[j] for j, name in enumerate(HEADERS)}
        email = fields["Email"].strip()
        if not is_reengage_eligible(fields, today=today):
            continue
        sendable, skip_reason = check_recipient_sendable(email)
        if not sendable:
            print(skip_reason)
            continue
        if is_suppressed is not None:
            try:
                if is_suppressed(email):
                    print(f"Skipped {email} (suppressed / do-not-contact)")
                    continue
            except Exception:
                pass
        leads.append(
            {
                "row_index": row_index,
                "reengage": True,
                "sent_was": fields["Sent"].strip(),
                **fields,
            }
        )
    return leads


def apply_reengage_reset(lead: dict, *, today: date | None = None) -> str:
    """Clear Sent and append history marker. Returns new Notes.

    DRY_RUN prints and does not write. Used by ``reengage --apply`` so the
    sheet visually matches the send list; ``send`` does not need this —
    ``get_unsent_leads`` already includes eligible stale rows.
    """
    today = today or date.today()
    row_index = int(lead["row_index"])
    old_sent = (lead.get("sent_was") or lead.get("Sent") or "").strip()
    new_notes = append_reengage_notes(lead.get("Notes") or "", old_sent, today)
    if _dry_run():
        print(
            f"DRY_RUN: would reset Sent row {row_index} "
            f"({lead.get('Email', '')}) sent_was={old_sent}"
        )
        return new_notes
    ws = get_worksheet_safe("Agencies")
    update_cell_safe(ws, row_index, NOTES_COL, new_notes)
    update_cell_safe(ws, row_index, SENT_COL, "")
    print(f"reengage reset row {row_index} sent_was={old_sent}")
    return new_notes


def run_reengage(*, apply: bool = False, limit: int | None = None) -> dict:
    """List (and optionally reset) 2-month-silent Agencies leads.

    Does not send mail. ``send`` picks eligible leads up automatically.
    """
    today = date.today()
    days = reengage_stale_days()
    leads = get_reengage_leads(today=today)
    if limit is not None:
        leads = leads[: max(0, int(limit))]

    print(
        f"reengage: {len(leads)} Agencies lead(s) silent {days}+ days "
        f"(no reply / no meeting / no bounce-unsub markers)"
    )
    for lead in leads:
        print(
            f"  • row {lead['row_index']} {lead.get('Email', '')} "
            f"sent={lead.get('sent_was') or lead.get('Sent', '')}"
        )

    reset = 0
    if apply:
        if _dry_run():
            print(f"DRY_RUN: not clearing Sent ({len(leads)} would be reset)")
        for lead in leads:
            apply_reengage_reset(lead, today=today)
            reset += 1

    return {
        "eligible": len(leads),
        "reset": reset if apply and not _dry_run() else 0,
        "leads": leads,
        "stale_days": days,
        "apply": apply,
        "dry_run": _dry_run(),
    }
