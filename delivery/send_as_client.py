"""Send outreach FROM the client's Google Workspace domain."""

from __future__ import annotations

import argparse
import base64
import csv
import os
import sys
import time
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "targets"
LOGS_DIR = ROOT / "logs"


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _targets_path(client_email: str) -> Path:
    return TARGETS_DIR / f"{_safe(client_email)}_targets.csv"


def _sent_log_path(client_email: str) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR / f"{_safe(client_email)}_sent.csv"


def load_client_info(client_email: str) -> dict:
    from pipeline.to_sheets import connect

    email = client_email.strip().lower()
    gc = connect()
    sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
    ws = sh.worksheet("Clients")
    values = ws.get_all_values()
    headers = [h.strip() for h in values[0]]
    col = {h.lower(): i for i, h in enumerate(headers)}

    def get(row, *names):
        for n in names:
            i = col.get(n.lower())
            if i is not None and i < len(row):
                return (row[i] or "").strip()
        return ""

    for row in values[1:]:
        if get(row, "Email").lower() == email or get(row, "Workspace Email").lower() == email:
            return {
                "email": get(row, "Workspace Email") or get(row, "Email") or email,
                "name": get(row, "Name"),
                "company": get(row, "Company"),
                "calendly": get(row, "Calendly"),
                "pricing": get(row, "Pricing"),
                "domain_age_days": get(row, "Domain Age Days"),
                "warmup_tier": get(row, "Warmup Tier"),
            }
    raise RuntimeError(f"Client not found: {email}")


def warmup_daily_limit(client_info: dict, requested: int) -> int:
    """Cap sends by domain age / warmup tier."""
    tier = (client_info.get("warmup_tier") or "").strip().lower()
    age_raw = (client_info.get("domain_age_days") or "").strip()
    try:
        age = int(float(age_raw)) if age_raw else None
    except ValueError:
        age = None

    if not age and tier:
        if tier in ("new", "cold"):
            age = 15
        elif tier in ("warm",):
            age = 60
        else:
            age = 120

    if age is None:
        # try crude WHOIS (optional)
        age = _whois_age_days(client_info.get("email", "").split("@")[-1]) or 120

    if age < 30:
        cap = 10
    elif age < 90:
        cap = 25
    else:
        cap = 50
    return max(1, min(int(requested), cap))


def _whois_age_days(domain: str) -> int | None:
    if not domain:
        return None
    try:
        import whois  # type: ignore

        w = whois.whois(domain)
        created = w.creation_date
        if isinstance(created, list):
            created = created[0]
        if not created:
            return None
        if created.tzinfo:
            created = created.replace(tzinfo=None)
        return (datetime.utcnow() - created).days
    except Exception:
        return None


def with_dentalmeet_utm(calendly_url: str) -> str:
    """Ensure Calendly booking link is tagged utm_source=dentalmeet."""
    url = (calendly_url or "").strip()
    if not url:
        return ""
    if "utm_source=dentalmeet" in url.lower():
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source=dentalmeet"


def build_outreach_email(
    target: dict, client_info: dict, opener: str = ""
) -> tuple[str, str, str]:
    """Personalized async dental-practice outreach from the agency client."""
    from pipeline.templates import build_dental_email

    first = (target.get("first_name") or "").strip() or "there"
    practice = (target.get("company") or "your practice").strip()
    opener = (opener or target.get("opener") or "").strip()

    client = dict(client_info)
    client["calendly"] = with_dentalmeet_utm(client_info.get("calendly") or "")

    return build_dental_email(
        client,
        first_name=first,
        practice=practice,
        opener=opener,
        service=(target.get("service") or "").strip(),
        city=(target.get("city") or "").strip(),
        competitor=(target.get("competitor") or "").strip(),
    )


def send_as_client(client_email: str, target: dict, email_content: dict) -> str:
    """Send one email via client's Gmail API credentials. Returns message id."""
    to = (target.get("email") or email_content.get("to") or "").strip()
    subject = email_content.get("subject") or ""
    body = email_content.get("body") or ""
    from_addr = (email_content.get("from") or client_email).strip()

    if os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"):
        print(f"DRY_RUN send_as {from_addr} → {to}: {subject}")
        return "dry-run"

    from googleapiclient.discovery import build

    from .connect_workspace import refresh_tokens

    creds = refresh_tokens(client_email)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = MIMEText(body, _charset="utf-8")
    msg["To"] = to
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    if email_content.get("thread_id") and email_content.get("in_reply_to"):
        msg["In-Reply-To"] = email_content["in_reply_to"]
        msg["References"] = email_content["in_reply_to"]

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    body_req: dict = {"raw": raw}
    if email_content.get("thread_id"):
        body_req["threadId"] = email_content["thread_id"]

    sent = (
        service.users()
        .messages()
        .send(userId="me", body=body_req)
        .execute()
    )
    return sent.get("id", "")


def _load_sent_emails(client_email: str) -> set[str]:
    path = _sent_log_path(client_email)
    if not path.exists():
        return set()
    out = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("email"):
                out.add(row["email"].strip().lower())
    return out


def _append_sent_log(client_email: str, row: dict) -> None:
    path = _sent_log_path(client_email)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        fields = [
            "date",
            "email",
            "company",
            "subject",
            "message_id",
            "wave",
            "template_id",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def run_campaign(client_email: str, daily_limit: int = 50) -> dict:
    """Send first-touch emails to unsent targets, respecting warmup."""
    client = load_client_info(client_email)
    limit = warmup_daily_limit(client, daily_limit)
    path = _targets_path(client_email)
    if not path.exists():
        raise FileNotFoundError(
            f"No targets file {path}. Run: python -m delivery.research_dental build_list {client_email}"
        )

    already = _load_sent_emails(client_email)
    sent = failed = skipped = 0

    with path.open(newline="", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))

    for t in targets:
        if sent >= limit:
            break
        email = (t.get("email") or "").strip().lower()
        if not email or email in already:
            skipped += 1
            continue
        subject, body, template_id = build_outreach_email(t, client, t.get("opener", ""))
        try:
            mid = send_as_client(
                client_email,
                t,
                {
                    "from": client.get("email") or client_email,
                    "to": email,
                    "subject": subject,
                    "body": body,
                },
            )
            _append_sent_log(
                client_email,
                {
                    "date": date.today().isoformat(),
                    "email": email,
                    "company": t.get("company", ""),
                    "subject": subject,
                    "message_id": mid,
                    "wave": "0",
                    "template_id": template_id,
                },
            )
            sent += 1
            already.add(email)
            print(f"[{sent}/{limit}] sent → {email}")
            if os.environ.get("DRY_RUN", "").lower() not in ("1", "true", "yes"):
                time.sleep(45)
        except Exception as e:
            failed += 1
            print(f"fail {email}: {e}")

    summary = {"sent": sent, "failed": failed, "skipped": skipped, "limit": limit}
    print("campaign summary:", summary)
    return summary


def send_followup(client_email: str, target: dict, followup_number: int) -> str:
    from pipeline.templates import (
        client_presentation_url,
        presentation_url,
    )

    client = load_client_info(client_email)
    first = (target.get("first_name") or "").strip() or "there"
    company = (target.get("company") or "").strip()
    practice = company or "your practice"
    cal = with_dentalmeet_utm(client.get("calendly") or "")
    preso = client_presentation_url(client)
    agent = (client.get("name") or "").strip() or "the team"
    agency = (client.get("company") or "").strip() or "our agency"

    if int(followup_number) == 1:
        subject = f"quick bump for {practice} (no call needed)"
        body = (
            f"Hi {first},\n\n"
            f"Just bumping this — still curious about more patient consultations?\n\n"
            f"We work 100% async. Read how it works:\n"
            f"{preso}\n\n"
            f"Book a time:\n"
            f"{cal}\n\n"
            f"— {agent}\n"
            f"{agency}"
        )
    else:
        subject = f"closing the loop / {practice}"
        body = (
            f"Hi {first},\n\n"
            f"Last note from me — no pressure either way.\n\n"
            f"If adding consultations is still on your radar, read the 2-min "
            f"presentation (no call needed):\n"
            f"{preso}\n\n"
            f"Or book a time to start async:\n"
            f"{cal}\n\n"
            f"Otherwise I won't follow up again.\n\n"
            f"— {agent}\n"
            f"{agency}"
        )
    _ = presentation_url  # import keeps parity with agency followups
    mid = send_as_client(
        client_email,
        target,
        {
            "from": client.get("email") or client_email,
            "to": target.get("email"),
            "subject": subject if not target.get("message_id") else f"Re: {subject}",
            "body": body,
            "in_reply_to": target.get("message_id"),
            "thread_id": target.get("thread_id"),
        },
    )
    _append_sent_log(
        client_email,
        {
            "date": date.today().isoformat(),
            "email": target.get("email", ""),
            "company": company,
            "subject": subject,
            "message_id": mid,
            "wave": str(followup_number),
        },
    )
    return mid


def run_followups(client_email: str, wave: int = 1) -> dict:
    """Follow up targets sent 3+ (wave1) or 7+ (wave2) days ago without later waves."""
    path = _sent_log_path(client_email)
    if not path.exists():
        print("no sent log")
        return {"sent": 0}

    today = date.today()
    need_days = 3 if wave == 1 else 7
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    by_email: dict[str, list[dict]] = {}
    for r in rows:
        by_email.setdefault((r.get("email") or "").lower(), []).append(r)

    sent = 0
    for email, hist in by_email.items():
        waves = {str(h.get("wave", "0")) for h in hist}
        if str(wave) in waves:
            continue
        if wave == 2 and "1" not in waves:
            continue
        firsts = [h for h in hist if str(h.get("wave", "0")) == "0"]
        if not firsts:
            continue
        try:
            sent_on = date.fromisoformat(firsts[0]["date"][:10])
        except Exception:
            continue
        if (today - sent_on).days < need_days:
            continue
        target = {
            "email": email,
            "first_name": "",
            "company": firsts[0].get("company", ""),
            "message_id": firsts[0].get("message_id", ""),
        }
        try:
            send_followup(client_email, target, wave)
            sent += 1
            print(f"FU{wave} → {email}")
            time.sleep(30)
        except Exception as e:
            print(f"FU fail {email}: {e}")
    return {"sent": sent, "wave": wave}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send outreach as client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run daily first-touch campaign")
    p_run.add_argument("client_email")
    p_run.add_argument("--limit", type=int, default=50)

    p_fu = sub.add_parser("followup", help="Send follow-up wave")
    p_fu.add_argument("client_email")
    p_fu.add_argument("--wave", type=int, choices=[1, 2], default=1)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        run_campaign(args.client_email, daily_limit=args.limit)
        return 0
    if args.cmd == "followup":
        run_followups(args.client_email, wave=args.wave)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
