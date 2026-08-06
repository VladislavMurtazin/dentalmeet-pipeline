"""Append personalized leads into Google Sheets (Pilot CRM)."""

import json
import os
import re
from pathlib import Path

import gspread

ROOT = Path(__file__).resolve().parent.parent
SA_FILE = ROOT / "service_account.json"

HEADERS = [
    "Company",
    "Contact",
    "Title",
    "Email",
    "Verified",
    "Signal",
    "Sent",
    "Replied",
    "Reply type",
    "Meeting",
    "Notes",
    "Approved",
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", re.I)


def connect():
    """Auth via local file or GOOGLE_SERVICE_ACCOUNT JSON env var."""
    if SA_FILE.exists():
        return gspread.service_account(filename=str(SA_FILE))
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        raise RuntimeError(
            "No service_account.json and GOOGLE_SERVICE_ACCOUNT is empty"
        )
    return gspread.service_account_from_dict(json.loads(raw))


def get_worksheet(ws_name: str | None = None):
    """Open sheet/worksheet; create worksheet + header if needed."""
    gc = connect()
    sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
    ws_name = ws_name or os.environ.get("WORKSHEET_NAME", "Agencies")
    sh = gc.open(sheet_name)
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=len(HEADERS))
        ws.update(range_name="A1:L1", values=[HEADERS], value_input_option="USER_ENTERED")
        return ws

    # Ensure header row exists
    values = ws.get_all_values()
    if not values:
        ws.update(range_name="A1:L1", values=[HEADERS], value_input_option="USER_ENTERED")
    return ws


_ws = None


def _ws_cached():
    global _ws
    if _ws is None:
        _ws = get_worksheet()
    return _ws


def is_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def _existing_emails(ws) -> set[str]:
    """Emails already in column D (skip header)."""
    col = ws.col_values(4)
    return {e.strip().lower() for e in col[1:] if e and e.strip()}


def _next_row(ws) -> int:
    """First empty cell in column A after the header."""
    values = ws.get_all_values()
    if not values:
        ws.update(range_name="A1:L1", values=[HEADERS], value_input_option="USER_ENTERED")
        return 2
    for i, row in enumerate(values[1:], start=2):
        a = row[0].strip() if row else ""
        if not a:
            return i
    return len(values) + 1


def _pad12(record: list[str]) -> list[str]:
    row = [(c if c is not None else "") for c in record[:12]]
    while len(row) < 12:
        row.append("")
    return row


def add_row(person: dict, opener: str) -> bool:
    """Write one lead into A..L at the next free row. Skip dup emails."""
    email = (person.get("email") or "").strip()
    if not email or not is_email(email):
        print(f"skip (bad email): {email!r}")
        return False

    ws = _ws_cached()
    if email.lower() in _existing_emails(ws):
        print(f"skip (dup): {email}")
        return False

    org = person.get("organization") or {}
    contact = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
    verified = "yes" if person.get("verified_email") else "no"
    domain = org.get("primary_domain", "") or ""

    record = _pad12(
        [
            org.get("name", ""),
            contact,
            person.get("title", ""),
            email,
            verified,
            opener or "",
            "",
            "",
            "",
            "",
            domain,
            "",  # Approved
        ]
    )

    row = _next_row(ws)
    ws.update(
        range_name=f"A{row}:L{row}",
        values=[record],
        value_input_option="USER_ENTERED",
    )
    return True
