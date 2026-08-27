"""Deliverability testing — seed emails and spam-folder detection."""

from __future__ import annotations

import email
import imaplib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "deliverability_state.json"
TEST_SUBJECT = "[DentalMeet deliverability test]"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def last_test_folder() -> str | None:
    """Return last recorded folder: inbox, spam, or unknown."""
    return _load_state().get("last_folder")


def record_test_result(folder: str, *, subject: str = TEST_SUBJECT) -> None:
    """Manually or via IMAP record where the test email landed."""
    data = _load_state()
    data["last_folder"] = folder.strip().lower()
    data["last_subject"] = subject
    data["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_state(data)
    print(f"deliverability: recorded folder={folder}")


def _alert_email() -> str:
    return os.environ.get("ALERT_EMAIL", "").strip()


def _imap_creds() -> tuple[str, str, str]:
    host = os.environ.get("ALERT_IMAP_SERVER", "imap.gmail.com").strip()
    addr = os.environ.get("ALERT_IMAP_EMAIL", "").strip() or _alert_email()
    password = os.environ.get("ALERT_IMAP_PASSWORD", "").replace(" ", "").strip()
    if not addr or not password:
        raise RuntimeError(
            "Set ALERT_IMAP_EMAIL + ALERT_IMAP_PASSWORD (or ALERT_EMAIL inbox app password)"
        )
    return host, addr, password


def _find_test_in_mailbox(imap: imaplib.IMAP4_SSL, mailbox: str) -> bool:
    status, _ = imap.select(mailbox, readonly=True)
    if status != "OK":
        return False
    status, data = imap.search(None, f'(SUBJECT "{TEST_SUBJECT}")')
    if status != "OK" or not data or not data[0]:
        return False
    return bool(data[0].split())


def check_inbox_placement() -> str:
    """
    Search ALERT_EMAIL inbox and Spam for the test subject.
    Returns: inbox | spam | not_found
    """
    host, addr, password = _imap_creds()
    imap = imaplib.IMAP4_SSL(host)
    try:
        imap.login(addr, password)
        spam_boxes = ['"[Gmail]/Spam"', "Spam", "[Google Mail]/Spam"]
        for box in spam_boxes:
            try:
                if _find_test_in_mailbox(imap, box):
                    return "spam"
            except imaplib.IMAP4.error:
                continue
        if _find_test_in_mailbox(imap, "INBOX"):
            return "inbox"
        return "not_found"
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def run_deliverability_test(*, check_imap: bool = True) -> dict:
    """Send test email to ALERT_EMAIL and optionally check IMAP placement."""
    from .alerts import alert_email, alert_from_email
    from .email_sender import send_email

    to = _alert_email()
    if not to:
        raise RuntimeError("ALERT_EMAIL must be set for deliverability test")

    body = (
        "This is an automated deliverability test from DentalMeet pipeline.\n"
        "If you see this in Spam, the pipeline will alert you.\n"
    )
    ok = send_email(
        to,
        TEST_SUBJECT,
        body,
        from_email=alert_from_email(),
        skip_unsubscribe=True,
    )
    result = {"sent": ok, "folder": None}

    if check_imap and ok:
        try:
            folder = check_inbox_placement()
            result["folder"] = folder
            if folder in ("inbox", "spam"):
                record_test_result(folder)
        except Exception as e:
            result["imap_error"] = str(e)
            print(f"deliverability IMAP check skipped: {e}")

    if result.get("folder") == "spam":
        from .alerts import _mark_alert_sent, send_alert

        send_alert(
            "Test email went to Spam",
            "Deliverability test detected in Spam folder after send.",
            severity="critical",
        )
        _mark_alert_sent("deliverability_spam")

    return result
