"""Plan B: send dental clinic outreach from Google Workspace (@meet-agency.com).

Clinic sends rotate across Google SMTP inboxes (health-weighted) so one
account does not blast the whole daily batch. Reply-To matches From.
Pin one inbox with FALLBACK_ROTATE=0 and FALLBACK_FROM_EMAIL=outreach@meet-agency.com.

Live sends require a Google SMTP password. Missing passwords log ERROR and
do not fall back to Zoho. Agency cold first-touch (pipeline campaign) also
prefers Google when PREFER_GOOGLE_SENDERS=1; this module is Plan B clinics.
"""

from __future__ import annotations

import argparse
import csv
import math
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

DEFAULT_FALLBACK_FROM = "outreach@meet-agency.com"


def _rotate_enabled() -> bool:
    return os.environ.get("FALLBACK_ROTATE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _fallback_from() -> str:
    return os.environ.get("FALLBACK_FROM_EMAIL", DEFAULT_FALLBACK_FROM).strip()


def _fallback_pool() -> list[str]:
    raw = os.environ.get("FALLBACK_FROM_POOL", "").strip()
    return [_norm(x) for x in raw.split(",") if _norm(x)]


def _refuse_zoho(context: str) -> None:
    print(
        f"ERROR: {context} require a Google Workspace "
        f"(@meet-agency.com) SMTP password (GOOGLE_SMTP_* / "
        f"GOOGLE_SMTP_ACCOUNTS). Refusing Zoho fallback."
    )


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _targets_path(client_email: str) -> Path:
    return TARGETS_DIR / f"{_safe(client_email)}_targets.csv"


def _sent_log_path(client_email: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{_safe(client_email)}_fallback_sent.csv"


def load_fallback_senders() -> list[str]:
    """Google Workspace From addresses for Plan B clinic sends.

    Live sends need SMTP passwords. DRY_RUN may list catalog Google
    accounts without a password (same pattern as post-form).
    """
    from pipeline.conversation_owner import google_workspace_smtp_accounts

    google = [_norm(e) for e, _ in google_workspace_smtp_accounts() if e]
    fallback_from = _norm(_fallback_from())

    if not _rotate_enabled():
        if fallback_from and fallback_from in google:
            return [fallback_from]
        _refuse_zoho("Plan B clinic sends")
        return []

    pool = _fallback_pool()
    if pool:
        have = set(google)
        pooled = [e for e in pool if e in have]
        if pooled:
            return pooled
        print(
            "Plan B: FALLBACK_FROM_POOL unmatched — using all Google Workspace accounts"
        )

    if google:
        return google

    _refuse_zoho("Plan B clinic sends")
    return []


def pick_next_fallback_sender(
    accounts: list[str],
    *,
    per_run_counts: dict[str, int] | None = None,
    max_per_account: int | None = None,
) -> str | None:
    """Health-weighted Google sender via select_next_account; skip caps."""
    from pipeline.conversation_owner import (
        google_workspace_smtp_accounts,
        is_google_workspace_sender,
    )
    from pipeline.send_throttle import (
        is_account_available,
        remaining_capacity,
        select_next_account,
    )

    pool = [_norm(a) for a in accounts if a and is_google_workspace_sender(a)]
    if not pool:
        return None

    counts = per_run_counts if per_run_counts is not None else {}
    eligible = {
        a
        for a in pool
        if max_per_account is None or counts.get(a, 0) < max_per_account
    }
    if not eligible:
        return None

    pairs = [
        (e, p)
        for e, p in google_workspace_smtp_accounts()
        if _norm(e) in eligible
    ]
    if pairs:
        picked = select_next_account(pairs)
        if picked:
            return _norm(picked[0])
        return None

    # DRY_RUN / tests may pass catalog Google emails without password pairs.
    for sender in pool:
        if sender not in eligible:
            continue
        try:
            if not is_account_available(sender):
                print(f"Plan B rotate skip {sender} (limit/cooldown)")
                continue
            if remaining_capacity(sender) <= 0:
                continue
        except Exception as e:
            print(f"Plan B rotate capacity check {sender}: {e}")
        return sender
    return None


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
    practice = (
        target.get("company") or target.get("organization_name") or "your practice"
    ).strip()
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
    """Send via Google Workspace; Reply-To matches From so replies land there."""
    from pipeline.conversation_owner import is_google_workspace_sender
    from pipeline.email_sender import send_email

    to = (target.get("email") or "").strip()
    sender = _norm(from_email)
    if not sender:
        sender = pick_next_fallback_sender(load_fallback_senders()) or ""
    if sender and not is_google_workspace_sender(sender):
        print(
            f"send_as_fallback: ERROR refusing non-Google From {sender} for {to}"
        )
        return False
    if not sender:
        print(
            f"send_as_fallback: ERROR no Google Workspace SMTP "
            f"password — Refusing Zoho fallback for {to}"
        )
        return False
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
    from_email = None
    try:
        from pipeline.conversation_owner import resolve_post_form_from_email

        from_email = resolve_post_form_from_email(client)
    except Exception:
        from_email = None
    if not from_email:
        print(
            f"forward_reply_to_client: ERROR no Google Workspace SMTP "
            f"password — refusing Zoho fallback for {client}"
        )
        return False
    return send_email(
        client, subject, body, from_email=from_email, skip_unsubscribe=True
    )


def run_campaign(client_email: str, daily_limit: int = 25) -> dict:
    """Send first-touch emails via Plan B, rotating SMTP From each send."""
    from .send_as_client import warmup_daily_limit

    client = load_client_info(client_email)
    limit = warmup_daily_limit(client, daily_limit)
    path = _targets_path(client_email)
    if not path.exists():
        raise FileNotFoundError(
            f"No targets file {path}. Run research_dental build_list first."
        )

    accounts = load_fallback_senders()
    if not accounts:
        raise RuntimeError(
            "No Google Workspace SMTP accounts for Plan B clinic sends "
            "(set GOOGLE_SMTP_* / GOOGLE_SMTP_ACCOUNTS). Refusing Zoho fallback."
        )

    max_per_account = max(1, math.ceil(limit / len(accounts)))
    per_run: dict[str, int] = {a: 0 for a in accounts}
    print(
        f"Plan B Google rotation: {len(accounts)} accounts {accounts}; "
        f"limit={limit}, max_per_account={max_per_account}"
    )

    sent_path = _sent_log_path(client_email)
    already: set[str] = set()
    if sent_path.exists():
        with sent_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("email"):
                    already.add(row["email"].strip().lower())

    sent = failed = skipped = 0
    by_from: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))

    write_header = not sent_path.exists()
    with sent_path.open("a", newline="", encoding="utf-8") as log_f:
        fields = ["date", "email", "company", "subject", "mode", "from"]
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

            sender = pick_next_fallback_sender(
                accounts,
                per_run_counts=per_run,
                max_per_account=max_per_account,
            )
            if not sender:
                print("Plan B: stopping — no available senders left this run")
                break

            subject, body = build_fallback_email(t, client, t.get("opener", ""))
            try:
                ok = send_as_fallback(
                    client_email, t, subject, body, from_email=sender
                )
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
                        "from": sender,
                    }
                )
                sent += 1
                per_run[sender] = per_run.get(sender, 0) + 1
                by_from[sender] = by_from.get(sender, 0) + 1
                already.add(email)
                print(f"[fallback {sent}/{limit}] {sender} → {email}")
                if os.environ.get("DRY_RUN", "").lower() not in ("1", "true", "yes"):
                    time.sleep(45)
            except Exception as e:
                failed += 1
                print(f"fallback fail {email}: {e}")

    summary = {
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "limit": limit,
        "mode": "fallback",
        "by_from": by_from,
        "accounts": accounts,
    }
    print("fallback campaign summary:", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan B clinic outreach (Google Workspace)"
    )
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
