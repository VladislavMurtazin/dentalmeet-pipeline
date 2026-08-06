"""One-time cleanup: realign Pilot CRM / Agencies into columns A..L."""

from __future__ import annotations

import re

from dotenv import load_dotenv

load_dotenv()

from .to_sheets import HEADERS, get_worksheet, is_email

SEP_RE = re.compile(r"^-+$")


def _nonempty_cells(row: list[str]) -> list[str]:
    return [c.strip() for c in row if c and c.strip()]


def _extract_record(row: list[str]) -> dict | None:
    """Pull Company/Contact/Title/Email/Verified/Signal/Notes from a messy row."""
    cells = _nonempty_cells(row)
    if not cells:
        return None
    # Skip pure separator / URL-dump rows without an email
    email_idx = next((i for i, c in enumerate(cells) if is_email(c)), None)
    if email_idx is None:
        return None

    before = cells[:email_idx]
    email = cells[email_idx]
    after = cells[email_idx + 1 :]

    company = before[0] if len(before) > 0 else ""
    contact = before[1] if len(before) > 1 else ""
    title = before[2] if len(before) > 2 else ""
    # If more than 3 cells before email, keep first three only

    verified = ""
    rest = after
    if rest and rest[0].lower() in ("yes", "no", "manual"):
        verified = rest[0].lower()
        rest = rest[1:]

    notes = rest[-1] if rest else ""
    signal = ""
    if rest:
        # Next non-empty after verified → Signal (may equal notes if only one)
        candidate = rest[0]
        if len(rest) == 1:
            # Single trailing cell is usually the domain → Notes only
            if "." in candidate and " " not in candidate and "@" not in candidate:
                signal = ""
            else:
                signal = candidate
        else:
            signal = candidate
            # Don't let Sent/Replied blanks sneak in — already stripped
            # Notes stays as last cell; if signal == notes and there is a middle, ok

    # Drop separator-looking signals
    if SEP_RE.match(signal):
        signal = ""

    return {
        "Company": company,
        "Contact": contact,
        "Title": title,
        "Email": email,
        "Verified": verified or "no",
        "Signal": signal,
        "Sent": "",
        "Replied": "",
        "Reply type": "",
        "Meeting": "",
        "Notes": notes,
        "Approved": "",
    }


def _record_list(rec: dict) -> list[str]:
    return [rec.get(h, "") for h in HEADERS]


def _dedup(records: list[dict]) -> list[dict]:
    """Keep one row per email; prefer the one with a non-empty Signal."""
    best: dict[str, dict] = {}
    order: list[str] = []
    for rec in records:
        key = rec["Email"].strip().lower()
        if key not in best:
            best[key] = rec
            order.append(key)
            continue
        if rec.get("Signal") and not best[key].get("Signal"):
            best[key] = rec
    return [best[k] for k in order]


def _from_list1(ws_list1) -> list[dict]:
    """Read manual rows: Company, Contact, Title, Email, Notes."""
    values = ws_list1.get_all_values()
    if not values:
        return []
    out = []
    # Detect header vs data
    start = 0
    if values and any(is_email(c) for c in values[0]):
        start = 0
    elif values:
        # Likely a header row — skip if no email on row 1
        start = 1 if not any(is_email(c) for c in values[0]) else 0

    for row in values[start:]:
        cells = _nonempty_cells(row)
        if not cells:
            continue
        email_idx = next((i for i, c in enumerate(cells) if is_email(c)), None)
        if email_idx is None:
            continue
        before = cells[:email_idx]
        email = cells[email_idx]
        after = cells[email_idx + 1 :]
        company = before[0] if len(before) > 0 else ""
        contact = before[1] if len(before) > 1 else ""
        title = before[2] if len(before) > 2 else ""
        notes = after[-1] if after else ""
        out.append(
            {
                "Company": company,
                "Contact": contact,
                "Title": title,
                "Email": email,
                "Verified": "manual",
                "Signal": "",
                "Sent": "",
                "Replied": "",
                "Reply type": "",
                "Meeting": "",
                "Notes": notes,
                "Approved": "",
            }
        )
    return out


def normalize() -> None:
    sh = get_worksheet().spreadsheet
    ws = sh.worksheet("Agencies")
    try:
        ws_list1 = sh.worksheet("Лист1")
    except Exception:
        ws_list1 = None

    raw = ws.get_all_values()
    total_rows = len(raw)
    extracted: list[dict] = []
    junk = 0
    for row in raw:
        # Skip header-looking rows
        joined = " ".join(_nonempty_cells(row)).lower()
        if joined.startswith("company") and "email" in joined:
            continue
        rec = _extract_record(row)
        if rec is None:
            if _nonempty_cells(row):
                junk += 1
            continue
        extracted.append(rec)

    records = _dedup(extracted)
    kept_agencies = len(records)

    # Merge Лист1 manuals if email not already present
    manual_added = 0
    if ws_list1 is not None:
        existing = {r["Email"].lower() for r in records}
        for rec in _from_list1(ws_list1):
            if rec["Email"].lower() in existing:
                continue
            records.append(rec)
            existing.add(rec["Email"].lower())
            manual_added += 1

    # Rebuild Agencies: clear, resize to 12 cols, write header + rows
    ws.clear()
    # Resize: keep enough rows, exactly 12 columns
    target_rows = max(len(records) + 10, 100)
    ws.resize(rows=target_rows, cols=12)

    ws.update(range_name="A1:L1", values=[HEADERS], value_input_option="USER_ENTERED")
    if records:
        body = [_record_list(r) for r in records]
        end = 1 + len(body)
        ws.update(range_name=f"A2:L{end}", values=body, value_input_option="USER_ENTERED")

    print(
        f"normalize done: kept={len(records)} "
        f"(from Agencies={kept_agencies}, from Лист1={manual_added}), "
        f"junk_removed={junk}, raw_rows={total_rows}"
    )


def main() -> None:
    normalize()


if __name__ == "__main__":
    main()
