"""Weekly performance reports for active delivery clients."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "data" / "reports"


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _sent_log(client_email: str) -> Path:
    return LOGS_DIR / f"{_safe(client_email)}_sent.csv"


def _week_bounds(ref: date | None = None) -> tuple[date, date]:
    ref = ref or date.today()
    start = ref - timedelta(days=ref.weekday() + 7)  # previous Monday
    end = start + timedelta(days=6)
    return start, end


def collect_week_stats(client_email: str) -> dict:
    start, end = _week_bounds()
    path = _sent_log(client_email)
    sent_rows = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    d = date.fromisoformat((row.get("date") or "")[:10])
                except Exception:
                    continue
                if start <= d <= end:
                    sent_rows.append(row)

    meetings = []
    try:
        from .track_meetings import count_billable_meetings

        m = count_billable_meetings(client_email, period="week")
        meetings = m.get("meetings") or []
        billable = m.get("billable_amount", 0)
    except Exception as e:
        print(f"meetings lookup skipped: {e}")
        billable = 0

    return {
        "client_email": client_email,
        "start": start,
        "end": end,
        "emails_sent": len(sent_rows),
        "open_rate": None,  # no pixel tracking yet
        "replies": {"total": 0, "positive": 0, "questions": 0, "not_interested": 0},
        "meetings": len(meetings),
        "billable_amount": billable,
        "meeting_rows": meetings[:5],
        "top_targets": sent_rows[:5],
    }


def generate_weekly_report(client_email: str) -> Path:
    """Write a simple HTML report (no heavy PDF deps)."""
    stats = collect_week_stats(client_email)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = (
        REPORTS_DIR
        / f"{_safe(client_email)}_{stats['start']}_{stats['end']}.html"
    )
    open_pct = "n/a (tracking not enabled)"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Weekly Report</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;color:#222}}
.card{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}}
h1{{font-size:1.4rem}} .muted{{color:#666}}
</style></head><body>
<h1>Weekly Report</h1>
<p class="muted">{stats['start']} → {stats['end']} · {client_email}</p>
<div class="card">
  <p>📧 Emails sent: <b>{stats['emails_sent']}</b></p>
  <p>📈 Open rate: <b>{open_pct}</b></p>
  <p>💬 Replies: <b>{stats['replies']['total']}</b>
     ({stats['replies']['positive']} positive,
      {stats['replies']['questions']} questions)</p>
  <p>📅 Meetings booked: <b>{stats['meetings']}</b></p>
  <p>💵 Billable: <b>${stats['billable_amount']}</b></p>
</div>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    print(f"report written: {path}")
    return path


def send_weekly_report(client_email: str) -> bool:
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import connect

    stats = collect_week_stats(client_email)
    report_path = generate_weekly_report(client_email)

    # client name
    first = "there"
    try:
        gc = connect()
        sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = sh.worksheet("Clients")
        for row in ws.get_all_values()[1:]:
            if row and row[2].strip().lower() == client_email.lower():
                first = (row[1] or "").split()[0] or "there"
                break
    except Exception:
        pass

    open_txt = "n/a"
    subject = f"Weekly Report — {stats['start']} to {stats['end']}"
    body = (
        f"Hi {first},\n"
        f"\n"
        f"Here's your weekly report for {stats['start']} to {stats['end']}:\n"
        f"\n"
        f"📊 Emails sent: {stats['emails_sent']}\n"
        f"📈 Open rate: {open_txt}\n"
        f"💬 Replies: {stats['replies']['total']} "
        f"({stats['replies']['positive']} positive, "
        f"{stats['replies']['questions']} questions)\n"
        f"📅 Meetings booked: {stats['meetings']}\n"
        f"\n"
        f"Full HTML report saved on our side "
        f"({report_path.name}). Reply if you want it attached next week.\n"
        f"\n"
        f"Questions? Just reply here.\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    # Gmail API raw path doesn't attach easily here — send summary in body
    return send_email(client_email, subject, body)


def active_clients() -> list[str]:
    from pipeline.to_sheets import connect

    gc = connect()
    sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
    try:
        ws = sh.worksheet("Clients")
    except Exception:
        return []
    values = ws.get_all_values()
    if not values:
        return []
    headers = [h.strip().lower() for h in values[0]]
    email_i = headers.index("email") if "email" in headers else 2
    status_i = headers.index("status") if "status" in headers else None
    out = []
    for row in values[1:]:
        if len(row) <= email_i:
            continue
        email = row[email_i].strip().lower()
        if not email:
            continue
        status = (
            row[status_i].strip().lower()
            if status_i is not None and status_i < len(row)
            else ""
        )
        if status in ("paid", "active", "onboarded") or not status:
            out.append(email)
    return out


def send_all() -> None:
    for email in active_clients():
        try:
            send_weekly_report(email)
        except Exception as e:
            print(f"report fail {email}: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly client reports")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("send", help="Send weekly report to one client")
    p.add_argument("client_email")
    sub.add_parser("send_all", help="Send weekly reports to all active clients")
    args = parser.parse_args(argv)
    if args.cmd == "send":
        ok = send_weekly_report(args.client_email)
        return 0 if ok else 1
    if args.cmd == "send_all":
        send_all()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
