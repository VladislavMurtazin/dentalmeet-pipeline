"""Plan B: send dental outreach from DentalMeet Zoho accounts on behalf of client."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "targets"
LOGS_DIR = ROOT / "logs"

FALLBACK_FROM = os.environ.get(
    "FALLBACK_FROM_EMAIL", "hello@dentalmeet.agency"
).strip()


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _targets_path(client_email: str) -> Path:
    return TARGETS_DIR / f"{_safe(client_email)}_targets.csv"


def _sent_log_path(client_email: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{_safe(client_email)}_fallback_sent.csv"


def load_client_info(client_email: str) -> dict:
    from .send_as_client import load_client_info as _load

    info = _load(client_email)
    info.setdefault("title", "Founder")
    return info


def build_fallback_email(
    target: dict, client_info: dict, opener: str = ""
) -> tuple[str, str]:
    """Build subject/body signed as client, sent from our account."""
    first = (target.get("first_name") or "").strip() or "there"
    practice = (target.get("company") or target.get("organization_name") or "your practice").strip()
    owner = (client_info.get("name") or "").strip() or "Our team"
    owner_first = owner.split()[0] if owner else "Our team"
    company = (client_info.get("company") or "").strip() or "our agency"
    title = (client_info.get("title") or "Founder").strip()
    opener_line = (opener or target.get("opener") or "").strip()
    cal = (client_info.get("calendly") or "").strip()

    subject = f"quick idea for {practice}"
    intro = (
        f"Hi {first},\n\n"
        f"I'm reaching out on behalf of {company} — we help dental practices "
        f"grow with marketing that actually books consultations.\n\n"
    )
    if opener_line:
        intro += f"{opener_line}\n\n"
    body = (
        f"{intro}"
        f"If adding more qualified consultations is on your radar, here's a "
        f"2-minute overview (no call needed):\n"
        f"https://dentalmeet.agency/pilot\n\n"
    )
    if cal:
        body += f"Or book a time directly:\n{cal}\n\n"
    body += (
        f"— {owner_first}\n"
        f"{title} at {company}\n"
        f"(sent via DentalMeet on behalf of {company})\n"
    )
    return subject, body


def send_as_fallback(
    client_email: str,
    target: dict,
    subject: str,
    body: str,
    *,
    from_email: str | None = None,
) -> bool:
    """Send via Zoho; Reply-To stays on our inbox for forwarding."""
    from pipeline.email_sender import send_email

    to = (target.get("email") or "").strip()
    sender = (from_email or FALLBACK_FROM).strip()
    if os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"):
        print(f"DRY_RUN fallback {sender} → {to}: {subject}")
        return True
    return send_email(
        to,
        subject,
        body,
        from_email=sender,
        extra_headers={"Reply-To": sender},
    )


def forward_reply_to_client(
    client_email: str,
    practice_name: str,
    original_subject: str,
    original_body: str,
) -> bool:
    """Forward a Plan B reply from our inbox to the client."""
    from pipeline.email_sender import send_email

    client = client_email.strip().lower()
    practice = (practice_name or "a dental practice").strip()
    subject = f"[DentalMeet] New reply from {practice}"
    body = (
        f"You received a new reply on your DentalMeet campaign.\n\n"
        f"From: {practice}\n"
        f"Subject: {original_subject}\n\n"
        f"--- original message ---\n"
        f"{original_body}\n\n"
        f"Reply to the practice from your own inbox, or ask us to help draft a response.\n"
    )
    if os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"):
        print(f"DRY_RUN forward → {client}: {subject}")
        return True
    return send_email(client, subject, body, skip_unsubscribe=True)


def run_campaign(client_email: str, daily_limit: int = 25) -> dict:
    """Send first-touch emails via Plan B (Zoho fallback accounts)."""
    from .send_as_client import _load_sent_emails, warmup_daily_limit

    client = load_client_info(client_email)
    limit = warmup_daily_limit(client, daily_limit)
    path = _targets_path(client_email)
    if not path.exists():
        raise FileNotFoundError(
            f"No targets file {path}. Run research_dental build_list first."
        )

    sent_path = _sent_log_path(client_email)
    already: set[str] = set()
    if sent_path.exists():
        with sent_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("email"):
                    already.add(row["email"].strip().lower())

    sent = failed = skipped = 0
    with path.open(newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))

    write_header = not sent_path.exists()
    with sent_path.open("a", newline="", encoding="utf-8") as log_f:
        fields = ["date", "email", "company", "subject", "mode"]
        writer = csv.DictWriter(log_f, fieldnames=fields)
        if write_header:
            writer.writeheader()

        for t in targets:
            if sent >= limit:
                break
            email = (t.get("email") or "").strip().lower()
            if not email or email in already:
                skipped += 1
                continue
            subject, body = build_fallback_email(t, client, t.get("opener", ""))
            try:
                ok = send_as_fallback(client_email, t, subject, body)
                if not ok:
                    failed += 1
                    continue
                writer.writerow(
                    {
                        "date": date.today().isoformat(),
                        "email": email,
                        "company": t.get("company", ""),
                        "subject": subject,
                        "mode": "fallback",
                    }
                )
                sent += 1
                already.add(email)
                print(f"[fallback {sent}/{limit}] sent → {email}")
                if os.environ.get("DRY_RUN", "").lower() not in ("1", "true", "yes"):
                    time.sleep(45)
            except Exception as e:
                failed += 1
                print(f"fallback fail {email}: {e}")

    summary = {"sent": sent, "failed": failed, "skipped": skipped, "limit": limit, "mode": "fallback"}
    print("fallback campaign summary:", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan B outreach (Zoho fallback)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="Run fallback campaign for client")
    p.add_argument("client_email")
    p.add_argument("--limit", type=int, default=25)
    args = parser.parse_args(argv)
    if args.cmd == "run":
        run_campaign(args.client_email, daily_limit=args.limit)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
