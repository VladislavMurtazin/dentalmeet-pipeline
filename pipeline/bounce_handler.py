"""Automatic bounce detection, blocking, and sheet cleanup."""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
BOUNCED_PATH = ROOT / "data" / "bounced_emails.json"
BLOCKED_DOMAINS_PATH = ROOT / "data" / "blocked_domains.json"

DOMAIN_BLOCK_THRESHOLD = 3
BOUNCE_RATE_TARGET = 0.02

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

BOUNCE_SUBJECT_MARKERS = (
    "delivery failed",
    "undeliverable",
    "mail delivery failed",
    "delivery status notification",
    "returned mail",
    "failure notice",
)

RECIPIENT_PATTERNS = (
    re.compile(r"Final-Recipient:\s*rfc822;\s*([^\s<>;\r\n]+)", re.I),
    re.compile(r"Original-Recipient:\s*rfc822;\s*([^\s<>;\r\n]+)", re.I),
    re.compile(
        r"(?:could not be delivered|failed to deliver|delivery to the following "
        r"recipient failed|user unknown|address rejected)[^\n]{0,120}?"
        r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        re.I | re.S,
    ),
    re.compile(r"<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>", re.I),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utc_now().date().isoformat()


def normalize_email(addr: str) -> str:
    return (addr or "").strip().lower()


def domain_from_email(addr: str) -> str:
    email_norm = normalize_email(addr)
    if "@" not in email_norm:
        return ""
    return email_norm.split("@", 1)[1]


def _load_store(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_bounced() -> dict:
    return _load_store(BOUNCED_PATH)


def save_bounced(data: dict) -> None:
    _save_store(BOUNCED_PATH, data)


def load_blocked_domains() -> dict:
    return _load_store(BLOCKED_DOMAINS_PATH)


def save_blocked_domains(data: dict) -> None:
    _save_store(BLOCKED_DOMAINS_PATH, data)


def _our_addresses() -> set[str]:
    from .email_sender import load_smtp_accounts

    addrs = {e.lower() for e, _ in load_smtp_accounts()}
    for key in ("GMAIL_EMAIL", "ALERT_EMAIL", "ZOHO_EMAIL"):
        val = os.environ.get(key, "").strip().lower()
        if val:
            addrs.add(val)
    return addrs


def is_bounce_message(from_email: str, subject: str, body: str) -> bool:
    from_norm = normalize_email(from_email)
    if "mailer-daemon" in from_norm or "postmaster" in from_norm:
        return True
    blob = f"{subject} {body}".lower()
    return any(marker in blob for marker in BOUNCE_SUBJECT_MARKERS)


def extract_bounced_recipient(from_email: str, subject: str, body: str) -> str | None:
    """Extract the failed recipient from a bounce DSN message."""
    text = f"{subject}\n{body}"
    ours = _our_addresses()

    for pattern in RECIPIENT_PATTERNS:
        for match in pattern.finditer(text):
            candidate = normalize_email(match.group(1).strip("<>\"'"))
            if not EMAIL_RE.fullmatch(candidate):
                continue
            if candidate in ours:
                continue
            if "mailer-daemon" in candidate or "postmaster" in candidate:
                continue
            return candidate

    # Fallback: any email in body that isn't ours or the sender
    for match in EMAIL_RE.finditer(text):
        candidate = normalize_email(match.group(0))
        if candidate in ours:
            continue
        if candidate == normalize_email(from_email):
            continue
        if "mailer-daemon" in candidate or "postmaster" in candidate:
            continue
        return candidate
    return None


def bounces_for_domain(domain: str, bounced: dict | None = None) -> int:
    domain = (domain or "").strip().lower()
    if not domain:
        return 0
    bounced = bounced if bounced is not None else load_bounced()
    return sum(
        1 for addr in bounced if domain_from_email(addr) == domain
    )


def maybe_block_domain(domain: str) -> bool:
    """Block domain after DOMAIN_BLOCK_THRESHOLD bounces. Returns True if newly blocked."""
    domain = (domain or "").strip().lower()
    if not domain:
        return False
    blocked = load_blocked_domains()
    if domain in blocked:
        return False
    count = bounces_for_domain(domain)
    if count < DOMAIN_BLOCK_THRESHOLD:
        return False
    blocked[domain] = {
        "date": _today(),
        "bounce_count": count,
        "reason": f"{count} bounces from domain",
    }
    save_blocked_domains(blocked)
    print(f"blocked domain {domain} after {count} bounces")
    return True


def record_bounced_email(
    recipient: str,
    *,
    bounce_date: str | None = None,
    source: str = "imap",
) -> bool:
    """Add email to bounced list; block domain if threshold reached."""
    addr = normalize_email(recipient)
    if not addr or not EMAIL_RE.fullmatch(addr):
        return False
    if addr in _our_addresses():
        return False

    bounced = load_bounced()
    if addr in bounced:
        return False

    bounced[addr] = {
        "date": bounce_date or _today(),
        "recorded_at": _utc_now().isoformat(timespec="seconds"),
        "source": source,
    }
    save_bounced(bounced)
    print(f"recorded bounce: {addr} ({bounce_date or _today()})")

    domain = domain_from_email(addr)
    maybe_block_domain(domain)

    from .alerts import record_bounce

    record_bounce(1)
    return True


def is_bounced(addr: str) -> tuple[bool, str]:
    """Return (True, bounce_date) if recipient previously bounced."""
    email_norm = normalize_email(addr)
    entry = load_bounced().get(email_norm)
    if not entry:
        return False, ""
    return True, str(entry.get("date") or "")


def is_domain_blocked(domain: str) -> bool:
    return domain.strip().lower() in load_blocked_domains()


def check_recipient_sendable(addr: str) -> tuple[bool, str]:
    """
    Check bounced list and blocked domains before send.
    Returns (True, '') or (False, log_message).
    """
    email_norm = normalize_email(addr)
    if not email_norm:
        return False, "empty recipient"

    bounced, bounce_date = is_bounced(email_norm)
    if bounced:
        return False, f"Skipped {email_norm} (bounced on {bounce_date})"

    domain = domain_from_email(email_norm)
    if domain and is_domain_blocked(domain):
        blocked = load_blocked_domains().get(domain, {})
        block_date = blocked.get("date") or "unknown"
        return False, f"Skipped {email_norm} (domain {domain} blocked on {block_date})"

    return True, ""


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _message_body(msg: email.message.Message, limit: int = 8000) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype in ("text/plain", "text/html", "message/rfc822", "message/delivery-status"):
                try:
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        chunks.append(payload.decode(charset, errors="replace"))
                    else:
                        chunks.append(str(part.get_payload()))
                except Exception:
                    chunks.append(str(part.get_payload()))
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            chunks.append(
                payload.decode(charset, errors="replace") if payload else str(msg.get_payload())
            )
        except Exception:
            chunks.append(str(msg.get_payload()))
    text = re.sub(r"\s+", " ", " ".join(chunks))
    return text[:limit]


def _scan_imap_mailbox(
    *,
    host: str,
    user: str,
    password: str,
    days: int = 7,
    label: str = "",
) -> list[str]:
    """Scan one mailbox for bounce DSNs; return newly recorded recipient emails."""
    recorded: list[str] = []
    since = (_utc_now() - timedelta(days=max(1, days))).strftime("%d-%b-%Y")
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT", "993") or "993"))
        mail.login(user, password.replace(" ", ""))
        mail.select("INBOX")
        status, data = mail.search(None, f'(SINCE "{since}")')
        if status != "OK" or not data or not data[0]:
            return recorded

        for num in data[0].split():
            try:
                st, msg_data = mail.fetch(num, "(RFC822)")
                if st != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email.message_from_bytes(raw)
                from_addr = parseaddr(_decode_header(msg.get("From")))[1]
                subject = _decode_header(msg.get("Subject"))
                body = _message_body(msg)
                if not is_bounce_message(from_addr, subject, body):
                    continue
                recipient = extract_bounced_recipient(from_addr, subject, body)
                if not recipient:
                    continue
                received = _today()
                try:
                    dt = parsedate_to_datetime(msg.get("Date"))
                    if dt:
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        received = dt.date().isoformat()
                except Exception:
                    pass
                if record_bounced_email(
                    recipient,
                    bounce_date=received,
                    source=f"imap:{label or user}",
                ):
                    recorded.append(recipient)
            except Exception as e:
                print(f"bounce scan parse error ({label or user}): {e}")
    except Exception as e:
        print(f"bounce scan IMAP error ({label or user}): {e}")
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    return recorded


def scan_all_bounces(days: int = 7) -> dict:
    """Scan Zoho SMTP accounts + Gmail inbox for bounce notifications."""
    from .email_sender import load_smtp_accounts

    host = os.environ.get("IMAP_SERVER", "imap.zoho.com").strip() or "imap.zoho.com"
    all_recorded: list[str] = []

    for account_email, password in load_smtp_accounts():
        found = _scan_imap_mailbox(
            host=host,
            user=account_email,
            password=password,
            days=days,
            label=account_email,
        )
        all_recorded.extend(found)

    gmail = os.environ.get("GMAIL_EMAIL", "").strip()
    gmail_pwd = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if gmail and gmail_pwd:
        found = _scan_imap_mailbox(
            host="imap.gmail.com",
            user=gmail,
            password=gmail_pwd,
            days=days,
            label="gmail",
        )
        all_recorded.extend(found)

    summary = {
        "scanned_accounts": len(load_smtp_accounts()) + (1 if gmail and gmail_pwd else 0),
        "new_bounces": len(all_recorded),
        "emails": all_recorded,
    }
    print(
        f"bounce scan: {summary['new_bounces']} new from "
        f"{summary['scanned_accounts']} mailbox(es)"
    )
    return summary


def weekly_bounce_stats() -> dict:
    """Bounces and sends in the rolling 7-day window."""
    cutoff = _utc_now().date() - timedelta(days=7)
    bounced = load_bounced()
    bounces_week = 0
    for entry in bounced.values():
        try:
            d = date.fromisoformat(str(entry.get("date") or "")[:10])
            if d >= cutoff:
                bounces_week += 1
        except ValueError:
            continue

    from .alerts import load_daily_metrics

    metrics = load_daily_metrics()
    sends_week = 0
    for day_str, day_data in metrics.items():
        try:
            d = date.fromisoformat(day_str[:10])
            if d >= cutoff:
                sends_week += int((day_data or {}).get("emails_sent") or 0)
        except ValueError:
            continue

    rate = (bounces_week / sends_week) if sends_week else 0.0
    return {
        "sends_week": sends_week,
        "bounces_week": bounces_week,
        "rate": rate,
        "target": BOUNCE_RATE_TARGET,
    }


def print_bounce_stats() -> None:
    stats = weekly_bounce_stats()
    rate_pct = stats["rate"] * 100
    target_pct = stats["target"] * 100
    bounced_count = len(load_bounced())
    blocked_count = len(load_blocked_domains())
    print("=== BOUNCE STATS ===")
    print(
        f"Bounce rate this week: {rate_pct:.1f}% "
        f"({stats['bounces_week']}/{stats['sends_week']} sends, target: <{target_pct:.0f}%)"
    )
    print(f"Total bounced emails: {bounced_count}")
    print(f"Blocked domains: {blocked_count}")
    if blocked_count:
        for domain in sorted(load_blocked_domains()):
            print(f"  • {domain}")
    print("====================")


def mark_bounced_leads_in_sheet() -> dict:
    """
    Weekly cleanup: mark Agencies rows for bounced emails (keep history).
    Sets Reply type=bounced and appends status marker to Notes.
    """
    from .to_sheets import (
        HEADERS,
        get_all_values_safe,
        get_worksheet_safe,
        update_cell_safe,
    )

    bounced = load_bounced()
    if not bounced:
        print("bounce cleanup: no bounced emails on file")
        return {"marked": 0}

    ws = get_worksheet_safe("Agencies")
    values = get_all_values_safe(ws)
    if not values:
        return {"marked": 0}

    email_col = HEADERS.index("Email")
    reply_col = HEADERS.index("Reply type") + 1
    notes_col = HEADERS.index("Notes") + 1
    marked = 0
    today = _today()

    for i, raw in enumerate(values[1:], start=2):
        row = list(raw) + [""] * (len(HEADERS) - len(raw))
        addr = normalize_email(row[email_col] if email_col < len(row) else "")
        if not addr or addr not in bounced:
            continue
        bounce_date = bounced[addr].get("date") or today
        update_cell_safe(ws, i, reply_col, "bounced")
        notes = (row[notes_col - 1] if notes_col - 1 < len(row) else "").strip()
        marker = f"status=bounced {bounce_date}"
        if marker not in notes:
            new_notes = f"{notes}; {marker}".strip("; ").strip() if notes else marker
            update_cell_safe(ws, i, notes_col, new_notes)
        marked += 1
        print(f"bounce cleanup: marked row {i} {addr}")

    print(f"bounce cleanup: marked {marked} lead(s)")
    return {"marked": marked}


def simulate_bounce(recipient: str) -> bool:
    """Test helper: record a bounce without waiting for DSN."""
    return record_bounced_email(recipient, source="test")
