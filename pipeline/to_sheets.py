"""Append personalized leads into Google Sheets (Pilot CRM)."""

import json
import os
import re
import time
from pathlib import Path

import gspread

# Retry / throttle for Sheets quota (429)
SHEETS_RETRY_BACKOFF_SEC = (60, 90, 120, 180, 240, 300)
SHEETS_ROW_DELAY_SEC = 1.5

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
    "Enriched",
    "PersonID",
    "LinkedIn",
]

N_COLS = len(HEADERS)
# A=0 … O=14 for 15 columns
HEADER_RANGE = f"A1:{chr(ord('A') + N_COLS - 1)}1"

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


def _ensure_headers(ws) -> None:
    """Create or extend header row so all HEADERS columns exist."""
    values = ws.get_all_values()
    if not values:
        ws.update(
            range_name=HEADER_RANGE,
            values=[HEADERS],
            value_input_option="USER_ENTERED",
        )
        return

    current = list(values[0])
    # Pad / append any missing header names (backward compatible)
    changed = False
    for i, name in enumerate(HEADERS):
        if i < len(current):
            if not (current[i] or "").strip():
                current[i] = name
                changed = True
        else:
            current.append(name)
            changed = True
    # Ensure Enriched / PersonID / LinkedIn exist
    for extra in ("Enriched", "PersonID", "LinkedIn"):
        if extra not in current:
            current.append(extra)
            changed = True
    if changed:
        end_col = chr(ord("A") + len(current) - 1)
        ws.update(
            range_name=f"A1:{end_col}1",
            values=[current],
            value_input_option="USER_ENTERED",
        )


def get_worksheet(ws_name: str | None = None):
    """Open sheet/worksheet; create worksheet + header if needed."""
    gc = connect()
    sheet_name = os.environ.get("SHEET_NAME", "Pilot CRM")
    ws_name = ws_name or os.environ.get("WORKSHEET_NAME", "Agencies")
    sh = gc.open(sheet_name)
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=max(N_COLS, 16))
        ws.update(
            range_name=HEADER_RANGE,
            values=[HEADERS],
            value_input_option="USER_ENTERED",
        )
        return ws

    _ensure_headers(ws)
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
        _ensure_headers(ws)
        return 2
    for i, row in enumerate(values[1:], start=2):
        a = row[0].strip() if row else ""
        if not a:
            return i
    return len(values) + 1


def _pad_row(record: list[str], n: int = N_COLS) -> list[str]:
    row = [(c if c is not None else "") for c in record[:n]]
    while len(row) < n:
        row.append("")
    return row


SIGNAL_COL = HEADERS.index("Signal") + 1  # 1-based
ENRICHED_COL = HEADERS.index("Enriched") + 1


def update_signal(row_index: int, signal: str) -> None:
    """Write Signal column for an existing Agencies row."""
    ws = _ws_cached()
    ws.update_cell(int(row_index), SIGNAL_COL, signal or "")


def _is_sheets_quota_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "429" in msg or "quota exceeded" in msg or "rate limit" in msg:
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    return False


def _reset_ws_cache() -> None:
    global _ws
    _ws = None


def _norm_li(url: str) -> str:
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].rstrip("/")
    return raw.replace("https://", "").replace("http://", "").replace("www.", "")


def load_sheet_identity_index() -> dict[str, set[str]]:
    """PersonID / email / LinkedIn sets from Agencies (for sync-leads dedup)."""
    index = {"person_ids": set(), "emails": set(), "linkedins": set()}
    ws = _ws_cached()
    values = ws.get_all_values()
    if not values:
        return index
    header = [(h or "").strip() for h in values[0]]

    def col(name: str) -> int | None:
        try:
            return header.index(name)
        except ValueError:
            try:
                return HEADERS.index(name)
            except ValueError:
                return None

    i_email = col("Email")
    i_pid = col("PersonID")
    i_li = col("LinkedIn")
    for row in values[1:]:
        def cell(i: int | None) -> str:
            if i is None or i >= len(row):
                return ""
            return (row[i] or "").strip()

        email = cell(i_email).lower()
        pid = cell(i_pid)
        li = _norm_li(cell(i_li))
        if email:
            index["emails"].add(email)
        if pid:
            index["person_ids"].add(pid)
        if li:
            index["linkedins"].add(li)
    return index


def add_row(person: dict, opener: str = "") -> bool:
    """Write one lead into A..O at the next free row. Skip dup emails.

    ``opener`` maps to the Signal column; leave empty for lazy scrape at send.
    """
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
    enriched_raw = person.get("enriched")
    if enriched_raw is True or str(enriched_raw).upper() in ("TRUE", "YES", "1"):
        enriched = "TRUE"
    elif str(enriched_raw).upper() == "SEARCH":
        enriched = "SEARCH"
    else:
        enriched = "SEARCH"
    person_id = str(
        person.get("person_id") or person.get("id") or ""
    ).strip()
    linkedin = str(person.get("linkedin_url") or person.get("linkedin") or "").strip()

    record = _pad_row(
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
            enriched,
            person_id,
            linkedin,
        ]
    )

    row = _next_row(ws)
    end_col = chr(ord("A") + N_COLS - 1)
    ws.update(
        range_name=f"A{row}:{end_col}{row}",
        values=[record],
        value_input_option="USER_ENTERED",
    )
    return True


def add_row_with_retry(
    person: dict,
    opener: str = "",
    *,
    max_retries: int | None = None,
    row_delay: float | None = None,
) -> bool:
    """``add_row`` with 429 retry/backoff and inter-row throttle.

    Backoff sleeps: 60s, 90s, 120s, 180s, 240s, 300s (up to 6 retries).
    """
    email = (person.get("email") or "").strip() or "(no-email)"
    retries = (
        len(SHEETS_RETRY_BACKOFF_SEC)
        if max_retries is None
        else max(0, int(max_retries))
    )
    delay = SHEETS_ROW_DELAY_SEC if row_delay is None else float(row_delay)
    attempt = 0

    while True:
        try:
            ok = add_row(person, opener)
            if delay > 0:
                time.sleep(delay)
            status = "added" if ok else "skipped"
            print(f"sheets {status}: {email}", flush=True)
            return ok
        except Exception as e:
            if not _is_sheets_quota_error(e) or attempt >= retries:
                print(f"sheets FAILED {email}: {e}", flush=True)
                raise
            sleep_for = SHEETS_RETRY_BACKOFF_SEC[
                min(attempt, len(SHEETS_RETRY_BACKOFF_SEC) - 1)
            ]
            attempt += 1
            print(
                f"sheets 429 for {email}; retry {attempt}/{retries} "
                f"after {sleep_for}s",
                flush=True,
            )
            _reset_ws_cache()
            time.sleep(sleep_for)
