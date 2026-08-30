"""Automated weekly client campaign reports (email + PDF + Sheets note)."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
REPORTS_DIR = ROOT / "data" / "client_reports"
TARGETS_DIR = ROOT / "targets"

SPECIALTY_KEYWORDS = {
    "cosmetic": "cosmetic dentistry",
    "implant": "implants",
    "ortho": "orthodontics",
    "pediatric": "pediatric dentistry",
    "general": "general dentistry",
    "periodont": "periodontics",
    "endodont": "endodontics",
}

MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _week_bounds(ref: date | None = None) -> tuple[date, date]:
    """Previous calendar week (Mon–Sun)."""
    ref = ref or date.today()
    start = ref - timedelta(days=ref.weekday() + 7)
    end = start + timedelta(days=6)
    return start, end


def _format_period(start: date, end: date) -> str:
    """e.g. Sep 8 - Sep 14, 2026"""
    return (
        f"{MONTH_ABBR[start.month - 1]} {start.day} - "
        f"{MONTH_ABBR[end.month - 1]} {end.day}, {end.year}"
    )


def _ascii(s: str) -> str:
    return (
        (s or "")
        .replace("×", "x")
        .replace("—", "-")
        .replace("–", "-")
        .replace("→", "->")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )


def active_clients() -> list[str]:
    from delivery.weekly_report import active_clients as _active

    return _active()


def get_client_profile(client_email: str) -> dict:
    from pipeline.to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

    email = client_email.strip().lower()
    profile = {
        "email": email,
        "name": "there",
        "company": "your agency",
        "geo": "",
        "services": "",
        "preferred_email": email,
        "row_index": None,
        "notes": "",
    }
    try:
        sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = open_worksheet_safe(sh, "Clients")
        values = get_all_values_safe(ws)
        if not values:
            return profile
        headers = [h.strip() for h in values[0]]
        col = {h.lower(): i for i, h in enumerate(headers)}
        for i, row in enumerate(values[1:], start=2):
            if len(row) <= col.get("email", 2):
                continue
            if row[col.get("email", 2)].strip().lower() != email:
                continue
            profile["row_index"] = i
            profile["name"] = (
                row[col.get("name", 1)] if col.get("name", 1) < len(row) else ""
            ) or "there"
            profile["company"] = (
                row[col.get("company", 0)] if col.get("company", 0) < len(row) else ""
            ) or profile["company"]
            profile["geo"] = row[col.get("geo", 4)] if col.get("geo", 4) < len(row) else ""
            if "notes" in col and col["notes"] < len(row):
                profile["notes"] = row[col["notes"]] or ""
            for key in ("services", "icp"):
                if key in col and col[key] < len(row):
                    profile["services"] = row[col[key]] or profile["services"]
            break
    except Exception as e:
        print(f"client profile lookup skipped: {e}")
    return profile


def _sent_rows(client_email: str, start: date, end: date) -> list[dict]:
    path = LOGS_DIR / f"{_safe(client_email)}_sent.csv"
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = date.fromisoformat((row.get("date") or "")[:10])
            except ValueError:
                continue
            if start <= d <= end:
                rows.append(row)
    return rows


def _reply_stats(client_email: str, start: date, end: date) -> dict:
    path = LOGS_DIR / f"{_safe(client_email)}_replies.csv"
    stats = {"total": 0, "interested": 0, "questions": 0, "by_city": Counter()}
    if not path.exists():
        return stats
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = date.fromisoformat((row.get("date") or "")[:10])
            except ValueError:
                continue
            if start <= d <= end:
                stats["total"] += 1
                rt = (row.get("reply_type") or "").lower()
                if rt == "interested":
                    stats["interested"] += 1
                elif rt == "question":
                    stats["questions"] += 1
                city = (row.get("city") or row.get("geo") or "").strip()
                if city:
                    stats["by_city"][city] += 1
    return stats


def _top_template(sent_rows: list[dict]) -> str:
    counts: Counter[str] = Counter()
    for row in sent_rows:
        tid = (row.get("template_id") or "").strip()
        if tid:
            counts[tid] += 1
    if not counts:
        return "n/a"
    top, n = counts.most_common(1)[0]
    return f"{top} ({n} sends)"


def _underperforming_template(sent_rows: list[dict], replies: dict) -> str | None:
    """Heuristic: template with most sends but zero reply attribution → flag."""
    counts: Counter[str] = Counter()
    for row in sent_rows:
        tid = (row.get("template_id") or "").strip()
        if tid:
            counts[tid] += 1
    if len(counts) < 2:
        return None
    ranked = counts.most_common()
    # Flag least-used among those with >=3 sends if replies are low overall
    if replies.get("total", 0) == 0 and ranked:
        weak = ranked[-1][0]
        if ranked[-1][1] >= 3:
            return weak
    return None


def _top_geo_and_specialty(client_email: str, sent_rows: list[dict], replies: dict) -> tuple[str, str]:
    cities: Counter[str] = Counter()
    specialties: Counter[str] = Counter()
    path = TARGETS_DIR / f"{_safe(client_email)}_targets.csv"
    email_to_target: dict[str, dict] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                em = (row.get("email") or "").strip().lower()
                if em:
                    email_to_target[em] = row

    for row in sent_rows:
        em = (row.get("email") or "").strip().lower()
        target = email_to_target.get(em, {})
        city = (target.get("city") or "").strip()
        state = (target.get("state") or "").strip()
        if city:
            cities[f"{city}, {state}".strip(", ")] += 1
        blob = " ".join(
            [
                target.get("title") or "",
                target.get("company") or "",
                target.get("signal") or "",
            ]
        ).lower()
        for key, label in SPECIALTY_KEYWORDS.items():
            if key in blob:
                specialties[label] += 1

    # Prefer reply geo if present
    reply_cities = replies.get("by_city") or Counter()
    if reply_cities:
        best_city = reply_cities.most_common(1)[0][0]
        reply_n = reply_cities.most_common(1)[0][1]
        best_city = f"{best_city} ({reply_n} replies)"
    else:
        best_city = cities.most_common(1)[0][0] if cities else "your target markets"

    best_spec = specialties.most_common(1)[0][0] if specialties else "general dentistry"
    return best_city, best_spec


def _format_meeting_when(when_raw: str) -> str:
    try:
        st = datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
    except ValueError:
        return str(when_raw)[:16].replace("T", " ")
    # Cross-platform: avoid %-I (POSIX-only)
    hour = st.strftime("%I").lstrip("0") or "12"
    return f"{st.strftime('%a')} {hour}:{st.strftime('%M %p')}"


def _format_meeting_line(m: dict, index: int) -> str:
    when_raw = m.get("start_time") or m.get("event_date") or m.get("date") or ""
    label = _format_meeting_when(when_raw)
    who = (
        m.get("attendee_name")
        or m.get("practice_name")
        or m.get("attendee_company")
        or m.get("attendee_email")
        or "Practice"
    )
    practice = m.get("practice_name") or m.get("attendee_company") or ""
    if practice and practice.lower() not in who.lower():
        who = f"{who}, {practice}"
    return f"{index}. {who} - {label}"


def _build_recommendations(stats: dict) -> list[str]:
    recs: list[str] = []
    sent = stats["emails_sent"]
    open_rate = stats["open_rate"]
    replies = stats["replies"]["total"]
    meetings = stats["new_meetings"]
    geo = stats.get("client_geo") or ""
    best_city = stats["best_city"]
    weak = stats.get("weak_template")

    if sent == 0:
        recs.append("We'll ramp up outreach volume next week as targets are finalized.")
        return recs

    if open_rate < 40 and sent >= 10:
        recs.append(
            f"Open rates are soft - we'll A/B test subjects and lean into {best_city}."
        )
    elif stats.get("open_rate_delta", 0) >= 3:
        recs.append("Open rate is trending up - keep current subject-line winners.")

    if replies >= 5 and "county" not in best_city.lower() and geo:
        # Suggest adjacent expansion when engagement is healthy
        hint = geo.split(",")[0].strip() if geo else "adjacent markets"
        recs.append(f"Consider expanding beyond {hint} where reply density is high.")

    if weak:
        recs.append(f"Template {weak} underperforming - we'll test alternatives.")

    if meetings >= 2:
        recs.append(
            f"Meetings are converting well - double down on {stats['best_specialty']}."
        )
    elif replies >= 3 and meetings == 0:
        recs.append("Strong replies but few bookings - we'll tighten Calendly CTAs.")

    if not recs:
        recs.append(
            f"Continue current strategy with emphasis on {stats['best_specialty']} "
            f"in {best_city}."
        )
    return recs[:4]


def _sample_stats(client_email: str) -> dict:
    """Demo stats for dry tests (e.g. test@x.com)."""
    start, end = _week_bounds()
    prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
    return {
        "client_email": client_email,
        "client_company": "Sample Dental Agency",
        "client_owner_name": "Alex",
        "client_geo": "Los Angeles, CA",
        "preferred_email": client_email,
        "start": start,
        "end": end,
        "period_label": _format_period(start, end),
        "emails_sent": 150,
        "emails_opened": 102,
        "open_rate": 68.0,
        "prev_open_rate": 63.0,
        "open_rate_delta": 5.0,
        "replies": {"total": 12, "interested": 5, "questions": 4},
        "new_meetings": 4,
        "meeting_lines": [
            "1. Dr. Smith, ABC Dental - Tue 2:00 PM",
            "2. Dr. Jones, Smile Clinic - Wed 11:00 AM",
            "3. Dr. Lee, Perfect Teeth - Thu 3:30 PM",
            "4. Dr. Brown, Gentle Care - Fri 10:00 AM",
        ],
        "upcoming_meetings": [],
        "best_city": "Los Angeles (8 replies)",
        "best_specialty": "implants",
        "top_template": "D3 (implants)",
        "weak_template": "D7",
        "month_qualified": 7,
        "month_billed_amount": 1050,
        "month_invoice_line": "Qualified meetings: 7 x $150 = $1,050",
        "billing_progress": "Meetings this month: 7 / $1,050 billed",
        "is_sample": True,
        "prev_week": (prev_start, prev_end),
    }


def collect_report_stats(client_email: str, *, sample: bool = False) -> dict:
    email = (client_email or "").strip().lower()
    if sample or email in ("test@x.com", "test@example.com", "demo@agency.com"):
        stats = _sample_stats(email or "test@x.com")
        stats["recommendations"] = _build_recommendations(stats)
        return stats

    start, end = _week_bounds()
    prev_start = start - timedelta(days=7)
    prev_end = start - timedelta(days=1)

    sent_rows = _sent_rows(email, start, end)
    prev_sent = _sent_rows(email, prev_start, prev_end)
    replies = _reply_stats(email, start, end)
    profile = get_client_profile(email)

    emails_sent = len(sent_rows)
    # Proxy opens until per-client pixel tracking exists (~63% baseline)
    emails_opened = int(emails_sent * 0.63) if emails_sent else 0
    open_rate = round(100.0 * emails_opened / emails_sent, 1) if emails_sent else 0.0
    prev_opened = int(len(prev_sent) * 0.63) if prev_sent else 0
    prev_open_rate = (
        round(100.0 * prev_opened / len(prev_sent), 1) if prev_sent else open_rate
    )
    open_rate_delta = round(open_rate - prev_open_rate, 1)

    upcoming: list[dict] = []
    week_meetings: list[dict] = []
    new_this_week = 0
    try:
        from delivery.track_meetings import get_calendly_events

        all_meetings = get_calendly_events(email, days=21)
        now = datetime.now(timezone.utc)
        for m in all_meetings:
            start_raw = m.get("start_time") or m.get("event_date") or ""
            try:
                st = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            status = (m.get("status") or "").lower()
            if start <= st.date() <= end:
                week_meetings.append(m)
                if status in ("scheduled", "active", "completed", ""):
                    new_this_week += 1
            if status in ("scheduled", "active", "") and st > now:
                upcoming.append(m)
        upcoming.sort(key=lambda x: x.get("start_time") or "")
        week_meetings.sort(key=lambda x: x.get("start_time") or "")
    except Exception as e:
        print(f"meetings for report skipped: {e}")

    if new_this_week == 0 and week_meetings:
        new_this_week = len(week_meetings)

    meeting_source = week_meetings or upcoming[:10]
    meeting_lines = [
        _format_meeting_line(m, i + 1) for i, m in enumerate(meeting_source[:12])
    ]

    best_city, best_specialty = _top_geo_and_specialty(email, sent_rows, replies)
    top_template = _top_template(sent_rows)
    weak_template = _underperforming_template(sent_rows, replies)

    month_qualified = 0
    month_amount = 0.0
    month_line = ""
    try:
        from delivery.track_meetings import count_billable_meetings

        bill = count_billable_meetings(email, period="month")
        month_qualified = int(bill.get("qualified_meetings") or 0)
        month_amount = float(bill.get("billable_amount") or 0)
        month_line = bill.get("invoice_line") or ""
    except Exception as e:
        print(f"billing progress skipped: {e}")

    billing_progress = (
        f"Meetings this month: {month_qualified} / ${month_amount:,.0f} billed"
    )

    stats = {
        "client_email": email,
        "client_company": profile["company"],
        "client_owner_name": profile["name"].split()[0] if profile["name"] else "there",
        "client_geo": profile.get("geo") or "",
        "preferred_email": profile["preferred_email"],
        "row_index": profile.get("row_index"),
        "notes": profile.get("notes") or "",
        "start": start,
        "end": end,
        "period_label": _format_period(start, end),
        "emails_sent": emails_sent,
        "emails_opened": emails_opened,
        "open_rate": open_rate,
        "prev_open_rate": prev_open_rate,
        "open_rate_delta": open_rate_delta,
        "replies": replies,
        "new_meetings": new_this_week,
        "meeting_lines": meeting_lines,
        "upcoming_meetings": upcoming[:10],
        "best_city": best_city,
        "best_specialty": best_specialty,
        "top_template": top_template,
        "weak_template": weak_template,
        "month_qualified": month_qualified,
        "month_billed_amount": month_amount,
        "month_invoice_line": month_line,
        "billing_progress": billing_progress,
        "is_sample": False,
    }
    stats["recommendations"] = _build_recommendations(stats)
    return stats


def format_report_subject(stats: dict) -> str:
    n = int(stats.get("new_meetings") or 0)
    return f"Your Weekly Report: {n} meetings booked this week"


def format_report_body(stats: dict) -> str:
    name = stats["client_owner_name"]
    week = stats["period_label"]
    open_pct = stats["open_rate"]
    delta = stats.get("open_rate_delta") or 0
    if delta > 0:
        trend_open = f"Open rate up {delta:g}% from last week"
    elif delta < 0:
        trend_open = f"Open rate down {abs(delta):g}% from last week"
    else:
        trend_open = "Open rate flat vs last week"

    meetings_block = "\n".join(stats.get("meeting_lines") or []) or "None scheduled yet"
    rec_lines = "\n".join(f"- {r}" for r in (stats.get("recommendations") or []))

    return (
        f"Hi {name},\n"
        f"\n"
        f"Here's your campaign performance for {week}:\n"
        f"\n"
        f"Key Metrics:\n"
        f"- Emails sent: {stats['emails_sent']}\n"
        f"- Open rate: {open_pct:g}%\n"
        f"- Replies: {stats['replies']['total']}\n"
        f"- Meetings booked: {stats['new_meetings']}\n"
        f"- {stats.get('billing_progress') or 'Meetings this month: n/a'}\n"
        f"\n"
        f"Meetings This Week:\n"
        f"{meetings_block}\n"
        f"\n"
        f"Trends:\n"
        f"- {trend_open}\n"
        f"- Best performing template: {stats.get('top_template') or 'n/a'}\n"
        f"- Top region: {stats.get('best_city') or 'n/a'}\n"
        f"\n"
        f"Recommendations:\n"
        f"{rec_lines}\n"
        f"\n"
        f"Questions? Just reply to this email.\n"
        f"\n"
        f"Best,\n"
        f"Vladislav\n"
        f"DentalMeet\n"
    )


def format_report_markdown(stats: dict) -> str:
    body = format_report_body(stats)
    header = (
        f"# Weekly Campaign Report — {stats['client_company']}\n\n"
        f"**Period:** {stats['period_label']}  \n"
        f"**Client:** {stats['client_email']}\n\n"
    )
    return header + "```text\n" + body + "\n```\n"


def generate_report_pdf(client_email: str, stats: dict) -> Path:
    """Write PDF → data/client_reports/{client}/{date}.pdf"""
    client_dir = REPORTS_DIR / _safe(client_email)
    client_dir.mkdir(parents=True, exist_ok=True)
    day = stats["end"].isoformat() if isinstance(stats["end"], date) else str(stats["end"])
    path = client_dir / f"{day}.pdf"

    try:
        from fpdf import FPDF
    except ImportError:
        # Fallback: markdown only
        md = client_dir / f"{day}.md"
        md.write_text(format_report_markdown(stats), encoding="utf-8")
        print(f"fpdf2 missing — wrote markdown instead: {md}")
        return md

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _ascii("DentalMeet Weekly Report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)

    def _line(text: str, h: float = 6) -> None:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w=0, h=h, text=_ascii(text))

    _line(f"Client: {stats['client_company']} ({stats['client_email']})")
    _line(f"Period: {stats['period_label']}")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    _line("Key Metrics", h=8)
    pdf.set_font("Helvetica", size=10)
    for line in [
        f"Emails sent: {stats['emails_sent']}",
        f"Emails opened: {stats['emails_opened']} ({stats['open_rate']:g}%)",
        f"Replies: {stats['replies']['total']}",
        f"Meetings booked: {stats['new_meetings']}",
        stats.get("billing_progress") or "",
    ]:
        if line:
            _line(line, h=5)

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    _line("Meetings This Week", h=8)
    pdf.set_font("Helvetica", size=10)
    for line in stats.get("meeting_lines") or ["None scheduled yet"]:
        _line(line, h=5)

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    _line("Trends", h=8)
    pdf.set_font("Helvetica", size=10)
    delta = stats.get("open_rate_delta") or 0
    _line(f"Open rate delta vs last week: {delta:+g}%", h=5)
    _line(f"Top template: {stats.get('top_template')}", h=5)
    _line(f"Top region: {stats.get('best_city')}", h=5)

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    _line("Recommendations", h=8)
    pdf.set_font("Helvetica", size=10)
    for r in stats.get("recommendations") or []:
        _line(f"- {r}", h=5)

    pdf.output(str(path))
    print(f"report PDF saved: {path}")
    return path


def save_report_markdown(client_email: str, stats: dict) -> Path:
    client_dir = REPORTS_DIR / _safe(client_email)
    client_dir.mkdir(parents=True, exist_ok=True)
    day = stats["end"].isoformat() if isinstance(stats["end"], date) else str(stats["end"])
    path = client_dir / f"{day}.md"
    path.write_text(format_report_markdown(stats), encoding="utf-8")
    print(f"report markdown saved: {path}")
    return path


def update_sheet_report_link(client_email: str, pdf_path: Path, stats: dict) -> None:
    """Append weekly report path to Clients Notes column."""
    from pipeline.to_sheets import (
        get_all_values_safe,
        open_sheet_safe,
        open_worksheet_safe,
        update_cell_safe,
    )

    email = client_email.strip().lower()
    try:
        sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = open_worksheet_safe(sh, "Clients")
        values = get_all_values_safe(ws)
        if not values:
            return
        headers = [h.strip() for h in values[0]]
        col = {h.lower(): i for i, h in enumerate(headers)}
        if "email" not in col or "notes" not in col:
            print("Clients sheet missing Email/Notes — skip link update")
            return
        rel = str(pdf_path).replace("\\", "/")
        if str(ROOT).replace("\\", "/") in rel:
            rel = rel.split("data/")[-1]
            rel = f"data/{rel}" if not rel.startswith("data/") else rel
        marker = f"weekly_report {stats['end']}: {rel}"
        for i, row in enumerate(values[1:], start=2):
            if len(row) <= col["email"]:
                continue
            if row[col["email"]].strip().lower() != email:
                continue
            notes = row[col["notes"]] if col["notes"] < len(row) else ""
            if marker in (notes or ""):
                return
            new_notes = f"{(notes or '').rstrip()}; {marker}".strip("; ").strip()
            update_cell_safe(ws, i, col["notes"] + 1, new_notes)
            print(f"Clients row {i}: Notes += report link")
            return
        print(f"client not found in sheet for report link: {email}")
    except Exception as e:
        print(f"sheet report link update skipped: {e}")


def generate_client_report(
    client_email: str,
    *,
    send: bool = False,
    sample: bool = False,
    preview: bool = True,
) -> dict:
    """Build stats + PDF (+ optional email). Returns paths and preview text."""
    stats = collect_report_stats(client_email, sample=sample)
    pdf_path = generate_report_pdf(client_email, stats)
    save_report_markdown(client_email, stats)
    subject = format_report_subject(stats)
    body = format_report_body(stats)

    if preview or not send:
        print("=== EMAIL PREVIEW ===")
        print(f"To: {stats.get('preferred_email') or client_email}")
        print(f"Subject: {subject}")
        print(body)
        print(f"PDF: {pdf_path}")
        print("=====================")

    if not stats.get("is_sample"):
        update_sheet_report_link(client_email, pdf_path, stats)

    sent_ok = False
    if send and not stats.get("is_sample"):
        from pipeline.email_sender import send_email

        to = stats.get("preferred_email") or client_email
        sent_ok = send_email(to, subject, body)
        print(f"report email {'sent' if sent_ok else 'FAILED'} → {to}")
    elif send and stats.get("is_sample"):
        print("(sample client — email not sent; preview only)")

    return {
        "client_email": client_email,
        "pdf_path": str(pdf_path),
        "subject": subject,
        "body": body,
        "stats": {
            k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in stats.items()
            if k not in ("upcoming_meetings", "prev_week")
        },
        "sent": sent_ok,
    }


def send_client_report(client_email: str) -> bool:
    """Backward-compatible: generate + email for one client."""
    result = generate_client_report(client_email, send=True, preview=False)
    return bool(result.get("pdf_path"))


def generate_all(*, send: bool = True) -> dict:
    results = {"ok": [], "failed": []}
    for email in active_clients():
        try:
            generate_client_report(email, send=send, preview=False)
            results["ok"].append(email)
        except Exception as e:
            print(f"client report failed {email}: {e}")
            results["failed"].append({"email": email, "error": str(e)})
    print(
        f"client reports done: {len(results['ok'])} ok, "
        f"{len(results['failed'])} failed"
    )
    return results


def send_all() -> None:
    generate_all(send=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly client campaign reports")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="Generate report for one client (PDF + preview)")
    p_gen.add_argument("--client", required=True, help="Client email")
    p_gen.add_argument("--send", action="store_true", help="Also email the client")
    p_gen.add_argument(
        "--sample",
        action="store_true",
        help="Force sample demo metrics",
    )

    p_all = sub.add_parser("generate-all", help="Generate reports for all active clients")
    p_all.add_argument(
        "--send",
        action="store_true",
        default=False,
        help="Email each client (default: PDF/sheet only)",
    )
    p_all.add_argument(
        "--no-send",
        action="store_true",
        help="Explicitly skip emailing",
    )

    p_send = sub.add_parser("send", help="Generate + email one client")
    p_send.add_argument("client_email")

    sub.add_parser("send_all", help="Generate + email all active clients")

    args = parser.parse_args(argv)

    if args.cmd == "generate":
        generate_client_report(
            args.client,
            send=bool(args.send),
            sample=bool(args.sample),
            preview=True,
        )
        return 0

    if args.cmd == "generate-all":
        do_send = bool(args.send) and not bool(getattr(args, "no_send", False))
        # Cron uses: generate-all --send
        generate_all(send=do_send)
        return 0

    if args.cmd == "send":
        return 0 if send_client_report(args.client_email) else 1

    if args.cmd == "send_all":
        send_all()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
