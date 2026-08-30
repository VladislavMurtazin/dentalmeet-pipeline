"""Send emails via Zoho Mail SMTP (SSL) with health-weighted account rotation.

Transport: smtp.zoho.com:465 (SMTP_SSL only). No third-party relays.
Accounts are chosen by deliverability health score and daily throttle limits.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import random
import re
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MAX_PER_RUN = 12
DELAY_MIN = 2
DELAY_MAX = 10

SMTP_SERVER = "smtp.zoho.com"
SMTP_PORT = 465  # SSL
MAX_SEND_ATTEMPTS = 3

UNSUBSCRIBE_URL = "https://dentalmeet.agency/unsubscribe"
UNSUBSCRIBE_MAILTO = "mailto:unsubscribe@dentalmeet.agency"
LIST_UNSUBSCRIBE_VALUE = f"<{UNSUBSCRIBE_MAILTO}>, <{UNSUBSCRIBE_URL}>"
LIST_UNSUBSCRIBE_POST = "List-Unsubscribe=One-Click"

UNSUBSCRIBE_PLAIN = (
    f"Unsubscribe: {UNSUBSCRIBE_URL}\n"
    'Not interested? Reply "unsubscribe" and we\'ll remove you.'
)
UNSUBSCRIBE_HTML = (
    f'<p style="font-size: 11px; color: #999;">'
    f'<a href="{UNSUBSCRIBE_URL}">Unsubscribe</a>'
    f"</p>"
)

ROOT = Path(__file__).resolve().parent.parent
ROTATION_PATH = ROOT / "data" / "smtp_rotation.json"

_SIG_LINE_RE = re.compile(r"(?m)^([—\-]\s+\S.*)$")


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _split_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def load_smtp_accounts() -> list[tuple[str, str]]:
    """Return [(email, app_password), ...] from env (same order)."""
    emails = _split_csv(os.environ.get("SMTP_ACCOUNTS", ""))
    passwords = _split_csv(os.environ.get("SMTP_APP_PASSWORDS", ""))

    # Back-compat: single Zoho/Gmail-style vars
    if not emails:
        single = (
            os.environ.get("ZOHO_EMAIL", "").strip()
            or os.environ.get("SMTP_EMAIL", "").strip()
            or os.environ.get("GMAIL_EMAIL", "").strip()
        )
        pwd = (
            os.environ.get("ZOHO_APP_PASSWORD", "").replace(" ", "").strip()
            or os.environ.get("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
            or os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
        )
        if single and pwd:
            return [(single, pwd)]
        return []

    pairs: list[tuple[str, str]] = []
    for i, email in enumerate(emails):
        if i >= len(passwords):
            print(f"WARN: no password for SMTP account {email} — skipping")
            continue
        pairs.append((email, passwords[i].replace(" ", "")))
    return pairs


def _load_rotation_index(n: int) -> int:
    if n <= 0:
        return 0
    if not ROTATION_PATH.exists():
        return 0
    try:
        data = json.loads(ROTATION_PATH.read_text(encoding="utf-8"))
        return int(data.get("next_index") or 0) % n
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _save_rotation_index(index: int, n: int) -> None:
    if n <= 0:
        return
    ROTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_PATH.write_text(
        json.dumps({"next_index": int(index) % n}, indent=2) + "\n",
        encoding="utf-8",
    )


def _next_account(
    accounts: list[tuple[str, str]],
) -> tuple[str, str, int] | None:
    """Pick next healthy account under daily limit (weighted by health score)."""
    from .send_throttle import select_next_account

    return select_next_account(accounts)


def _advance_rotation(used_index: int, n: int) -> None:
    _save_rotation_index(used_index + 1, n)


def _insert_unsubscribe_before_signature(body_text: str) -> str:
    """Put unsubscribe block at the bottom, BEFORE the signature line."""
    text = (body_text or "").rstrip()
    if UNSUBSCRIBE_URL in text:
        return text + "\n"

    matches = list(_SIG_LINE_RE.finditer(text))
    if matches:
        m = matches[-1]
        before = text[: m.start()].rstrip()
        after = text[m.start() :].lstrip()
        return f"{before}\n\n{UNSUBSCRIBE_PLAIN}\n\n{after}\n"

    return f"{text}\n\n{UNSUBSCRIBE_PLAIN}\n"


def _plain_to_simple_html(plain: str) -> str:
    """Minimal HTML twin + styled Unsubscribe link."""
    escaped = html_lib.escape(plain or "")
    body = escaped.replace("\n", "<br>\n")
    marker = html_lib.escape(f"Unsubscribe: {UNSUBSCRIBE_URL}")
    if marker in body:
        body = body.replace(marker, UNSUBSCRIBE_HTML, 1)
        reply = html_lib.escape(
            'Not interested? Reply "unsubscribe" and we\'ll remove you.'
        )
        body = body.replace(f"<br>\n{reply}", "", 1)
        body = body.replace(reply, "", 1)
    else:
        body = body + "<br>\n" + UNSUBSCRIBE_HTML
    return (
        "<!DOCTYPE html><html><body style=\"font-family: sans-serif; "
        "font-size: 14px; color: #222;\">"
        f"{body}"
        "</body></html>"
    )


def _apply_unsubscribe_headers(msg) -> None:
    """List-Unsubscribe BEFORE send (RFC 2369 + RFC 8058)."""
    msg["List-Unsubscribe"] = LIST_UNSUBSCRIBE_VALUE
    msg["List-Unsubscribe-Post"] = LIST_UNSUBSCRIBE_POST


def _build_message(
    to: str,
    subject: str,
    body_text: str,
    from_addr: str,
    *,
    skip_unsubscribe: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> MIMEMultipart:
    """Build multipart/alternative with unsubscribe headers set before send."""
    if skip_unsubscribe:
        plain = (body_text or "").rstrip() + "\n"
        escaped = html_lib.escape(plain)
        html_body = (
            "<!DOCTYPE html><html><body style=\"font-family: sans-serif; "
            "font-size: 14px; color: #222;\">"
            f"{escaped.replace(chr(10), '<br>' + chr(10))}"
            "</body></html>"
        )
    else:
        plain = _insert_unsubscribe_before_signature(body_text)
        html_body = _plain_to_simple_html(plain)

    msg = MIMEMultipart("alternative")
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    domain = (
        from_addr.split("@")[-1] if from_addr and "@" in from_addr else "zoho.com"
    )
    msg["Message-ID"] = make_msgid(domain=domain)
    if not skip_unsubscribe:
        _apply_unsubscribe_headers(msg)
    for key, value in (extra_headers or {}).items():
        if key and value and key not in msg:
            msg[key] = value

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _send_via_zoho(
    to: str,
    subject: str,
    body_text: str,
    email: str,
    password: str,
    *,
    skip_unsubscribe: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Send one message through Zoho SMTP SSL (raises on failure)."""
    msg = _build_message(
        to,
        subject,
        body_text,
        email,
        skip_unsubscribe=skip_unsubscribe,
        extra_headers=extra_headers,
    )
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.login(email, password)
        server.send_message(msg)


def send_email(
    to: str,
    subject: str,
    body_text: str,
    *,
    from_email: str | None = None,
    skip_unsubscribe: bool = False,
    skip_throttle: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> bool:
    """Send via Zoho SMTP with health-weighted rotation; retry next account on failure.

    If ``from_email`` is set, send only from that account (no rotation failover).
    ``skip_throttle`` bypasses daily limits (warmup / alerts).
    """
    to = (to or "").strip()
    subject = subject or ""
    body_text = body_text or ""
    from_email = (from_email or "").strip().lower() or None

    if not to:
        print("send_email error: empty recipient")
        return False

    from .bounce_handler import check_recipient_sendable

    sendable, skip_reason = check_recipient_sendable(to)
    if not sendable:
        print(skip_reason)
        return False

    accounts = load_smtp_accounts()
    if not accounts:
        print(
            "send_email error: set SMTP_ACCOUNTS + SMTP_APP_PASSWORDS "
            "(comma-separated, same order)"
        )
        return False

    from .send_throttle import is_account_available, record_send

    if from_email:
        match = next(
            ((i, e, p) for i, (e, p) in enumerate(accounts) if e.lower() == from_email),
            None,
        )
        if not match:
            print(f"send_email error: from_email {from_email} not in SMTP_ACCOUNTS")
            return False
        idx, email, password = match
        if not skip_throttle and not is_account_available(email):
            print(f"send_email: fixed account {email} unavailable (limit/cooldown)")
            return False
        if _dry_run():
            preview = _build_message(
                to,
                subject,
                body_text,
                email,
                skip_unsubscribe=skip_unsubscribe,
                extra_headers=extra_headers,
            )
            print("=== DRY_RUN email ===")
            print(f"Transport: Zoho SMTP_SSL {SMTP_SERVER}:{SMTP_PORT}")
            print(f"From (fixed): {email}")
            print(f"To: {to}")
            print(f"Subject: {subject}")
            if not skip_unsubscribe:
                print(f"List-Unsubscribe: {preview['List-Unsubscribe']}")
                print(f"List-Unsubscribe-Post: {preview['List-Unsubscribe-Post']}")
            print(preview.get_payload()[0].get_payload(decode=True).decode("utf-8"))
            print("=== end DRY_RUN ===")
            if not skip_throttle:
                record_send(email, to)
            return True
        try:
            _send_via_zoho(
                to,
                subject,
                body_text,
                email,
                password,
                skip_unsubscribe=skip_unsubscribe,
                extra_headers=extra_headers,
            )
            if not skip_throttle:
                record_send(email, to)
            print(f"sent via Zoho {email} (fixed account {idx + 1}/{len(accounts)})")
            return True
        except Exception as e:
            print(f"send_email Zoho fail fixed account {email}: {e}")
            return False

    if skip_throttle:
        # Fallback to sequential index for internal/warmup-style sends
        n = len(accounts)
        start = _load_rotation_index(n)
        email, password = accounts[start]
        idx = start
        if _dry_run():
            print(f"(DRY_RUN) skip_throttle from {email}")
            _advance_rotation(idx, n)
            return True
        try:
            _send_via_zoho(
                to,
                subject,
                body_text,
                email,
                password,
                skip_unsubscribe=skip_unsubscribe,
                extra_headers=extra_headers,
            )
            _advance_rotation(idx, n)
            print(f"sent via Zoho {email} (skip_throttle {idx + 1}/{n})")
            return True
        except Exception as e:
            print(f"send_email Zoho fail skip_throttle {email}: {e}")
            return False

    if _dry_run():
        picked = _next_account(accounts)
        if not picked:
            return False
        email, _, idx = picked
        preview = _build_message(
            to,
            subject,
            body_text,
            email,
            skip_unsubscribe=skip_unsubscribe,
            extra_headers=extra_headers,
        )
        print("=== DRY_RUN email ===")
        print(f"Transport: Zoho SMTP_SSL {SMTP_SERVER}:{SMTP_PORT}")
        print(f"From (weighted #{idx + 1}/{len(accounts)}): {email}")
        print(f"To: {to}")
        print(f"Subject: {subject}")
        if not skip_unsubscribe:
            print(f"List-Unsubscribe: {preview['List-Unsubscribe']}")
            print(f"List-Unsubscribe-Post: {preview['List-Unsubscribe-Post']}")
        print(preview.get_payload()[0].get_payload(decode=True).decode("utf-8"))
        print("=== end DRY_RUN ===")
        record_send(email, to)
        _advance_rotation(idx, len(accounts))
        return True

    n = len(accounts)
    attempts = min(MAX_SEND_ATTEMPTS, n)
    last_err: Exception | None = None
    tried: set[str] = set()

    for _attempt in range(attempts):
        picked = _next_account(accounts)
        if not picked:
            return False
        email, password, idx = picked
        if email.lower() in tried:
            # Avoid infinite same-pick loops; try sequential unused available
            from .send_throttle import is_account_available as _avail

            alt = next(
                (
                    (i, e, p)
                    for i, (e, p) in enumerate(accounts)
                    if e.lower() not in tried and _avail(e)
                ),
                None,
            )
            if not alt:
                print("All accounts at capacity, queued for tomorrow")
                return False
            idx, email, password = alt
        tried.add(email.lower())
        try:
            _send_via_zoho(
                to,
                subject,
                body_text,
                email,
                password,
                skip_unsubscribe=skip_unsubscribe,
                extra_headers=extra_headers,
            )
            record_send(email, to)
            _advance_rotation(idx, n)
            print(f"sent via Zoho {email} (account {idx + 1}/{n})")
            return True
        except Exception as e:
            last_err = e
            print(
                f"send_email Zoho fail account {idx + 1}/{n} ({email}): {e} "
                f"— trying next"
            )

    print(f"send_email error to {to}: all {attempts} Zoho attempts failed; last={last_err}")
    return False


def send_batch(
    recipients: list[dict],
    *,
    delay_min: int | None = None,
    delay_max: int | None = None,
    max_per_run: int | None = None,
) -> dict:
    """Send emails one-by-one with random delay; capped per run."""
    sent: list[dict] = []
    failed: list[dict] = []

    dmin = DELAY_MIN if delay_min is None else int(delay_min)
    dmax = DELAY_MAX if delay_max is None else int(delay_max)
    if dmin > dmax:
        dmin, dmax = dmax, dmin
    cap = MAX_PER_RUN if max_per_run is None else int(max_per_run)

    batch = list(recipients or [])[:cap]
    if len(recipients or []) > cap:
        print(
            f"send_batch: capping at {cap} emails "
            f"(got {len(recipients)}, rest skipped this run)"
        )

    skipped_bounce: list[dict] = []
    skipped_timezone: list[dict] = []
    skipped_throttle: list[dict] = []
    from .bounce_handler import check_recipient_sendable
    from .send_throttle import any_account_available, refresh_account_limits

    refresh_account_limits()

    for i, item in enumerate(batch):
        if not any_account_available():
            print("All accounts at capacity, queued for tomorrow")
            rest = batch[i:]
            skipped_throttle.extend(rest)
            break

        to = (item.get("to") or "").strip()
        sendable, skip_reason = check_recipient_sendable(to)
        if not sendable:
            skipped_bounce.append({**item, "skip_reason": skip_reason})
            print(f"[{i + 1}/{len(batch)}] {skip_reason}")
            continue

        tz_name = item.get("timezone")
        if tz_name:
            from .timezone_utils import can_send_now, format_local_datetime_safe, next_send_datetime

            if not can_send_now(tz_name):
                send_after = next_send_datetime(tz_name)
                when = format_local_datetime_safe(send_after, tz_name)
                skipped_timezone.append({**item, "skip_reason": f"outside send window → {when}"})
                print(f"[{i + 1}/{len(batch)}] skipped {to} (outside send window → {when})")
                continue

        subject = item.get("subject") or ""
        body = item.get("body") or ""

        ok = False
        try:
            ok = send_email(to, subject, body)
        except Exception as e:
            print(f"send_batch unexpected error for {to}: {e}")
            ok = False

        if ok:
            sent.append(item)
            sel_log = item.get("template_selection_log")
            if sel_log:
                print(sel_log)
            elif item.get("template_id"):
                print(f"Selected {item['template_id']}")
            print(f"[{i + 1}/{len(batch)}] sent: {to}")
        else:
            if not any_account_available():
                print("All accounts at capacity, queued for tomorrow")
                skipped_throttle.append(item)
                skipped_throttle.extend(batch[i + 1 :])
                break
            failed.append(item)
            print(f"[{i + 1}/{len(batch)}] failed: {to}")

        if i < len(batch) - 1:
            delay = random.randint(dmin, dmax)
            if _dry_run():
                print(f"(DRY_RUN) would sleep {delay}s")
            else:
                print(f"sleeping {delay}s before next send…")
                time.sleep(delay)

    return {
        "sent": sent,
        "failed": failed,
        "skipped_bounce": skipped_bounce,
        "skipped_timezone": skipped_timezone,
        "skipped_throttle": skipped_throttle,
    }
