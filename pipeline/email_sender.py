"""Send plain-text emails via Gmail REST API (HTTPS / OAuth2 user token).

Bypasses ISP SMTP blocks on ports 465/587 by using api.gmail.com over 443.
"""

from __future__ import annotations

import base64
import os
import random
import time
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MAX_PER_RUN = 12
DELAY_MIN = 45
DELAY_MAX = 90

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
ROOT = Path(__file__).resolve().parent.parent

_service = None


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _token_path() -> Path:
    raw = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json").strip()
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _from_addr() -> str:
    return os.environ.get("GMAIL_EMAIL", "").strip()


def _load_user_creds():
    """Load/refresh OAuth user credentials from gmail_token.json."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = _token_path()
    if not token_file.exists():
        raise RuntimeError(
            f"Missing {token_file.name}. Run once: "
            f"python -m pipeline.setup_gmail_oauth"
        )

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise RuntimeError(
            "Gmail OAuth token invalid/expired. Re-run: "
            "python -m pipeline.setup_gmail_oauth"
        )
    return creds


def _get_service():
    """Build a cached Gmail API client."""
    global _service
    if _service is not None:
        return _service
    from googleapiclient.discovery import build

    creds = _load_user_creds()
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def _build_raw_message(to: str, subject: str, body_text: str, from_addr: str) -> dict:
    """MIMEText → base64url raw payload for Gmail API (padding stripped)."""
    msg = MIMEText(body_text or "", _charset="utf-8")
    if from_addr:
        msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    domain = (from_addr.split("@")[-1] if from_addr and "@" in from_addr else "gmail.com")
    msg["Message-ID"] = make_msgid(domain=domain)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    return {"raw": raw}


def send_email(to: str, subject: str, body_text: str) -> bool:
    """Send one plain-text email via Gmail API. True on success."""
    to = (to or "").strip()
    subject = subject or ""
    body_text = body_text or ""

    if not to:
        print("send_email error: empty recipient")
        return False

    if _dry_run():
        print("=== DRY_RUN email ===")
        print(f"To: {to}")
        print(f"Subject: {subject}")
        print(body_text)
        print("=== end DRY_RUN ===")
        return True

    from_addr = _from_addr()
    try:
        service = _get_service()
        body = _build_raw_message(to, subject, body_text, from_addr)
        service.users().messages().send(userId="me", body=body).execute()
        return True
    except Exception as e:
        print(f"send_email error to {to}: {e}")
        return False


def send_batch(
    recipients: list[dict],
    *,
    delay_min: int | None = None,
    delay_max: int | None = None,
    max_per_run: int | None = None,
) -> dict:
    """Send emails one-by-one with random delay; capped per run.

    Each recipient dict: {"to": str, "subject": str, "body": str}
    Returns {"sent": [...], "failed": [...]} with the original dicts.
    """
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

    for i, item in enumerate(batch):
        to = (item.get("to") or "").strip()
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
            print(f"[{i + 1}/{len(batch)}] sent: {to}")
        else:
            failed.append(item)
            print(f"[{i + 1}/{len(batch)}] failed: {to}")

        if i < len(batch) - 1:
            delay = random.randint(dmin, dmax)
            if _dry_run():
                print(f"(DRY_RUN) would sleep {delay}s")
            else:
                print(f"sleeping {delay}s before next send…")
                time.sleep(delay)

    return {"sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# SMTP fallback (ISP often blocks 465/587). Kept for reference / emergency.
# ---------------------------------------------------------------------------
#
# import smtplib
# from email.mime.text import MIMEText
#
# def send_email_smtp(to, subject, body_text) -> bool:
#     from_addr = os.environ["GMAIL_EMAIL"]
#     password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
#     msg = MIMEText(body_text, _charset="utf-8")
#     msg["From"] = from_addr
#     msg["To"] = to
#     msg["Subject"] = subject
#     try:
#         with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
#             smtp.ehlo()
#             smtp.starttls()
#             smtp.ehlo()
#             smtp.login(from_addr, password)
#             smtp.sendmail(from_addr, [to], msg.as_string())
#         return True
#     except Exception as e:
#         print(f"send_email_smtp error to {to}: {e}")
#         return False
#
# Service-account attempt (does NOT work for consumer @gmail.com without
# Workspace domain-wide delegation):
#
# from google.oauth2 import service_account
# info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
# creds = service_account.Credentials.from_service_account_info(
#     info, scopes=SCOPES
# )
# # Would still need subject=GMAIL_EMAIL via domain-wide delegation.
