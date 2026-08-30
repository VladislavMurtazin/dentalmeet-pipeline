"""Automatic case study generation from successful client results."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CASE_STUDIES_PATH = ROOT / "data" / "case_studies.json"
CASE_STUDIES_DIR = ROOT / "data" / "case_studies"
TEMPLATE_PATH = ROOT / "data" / "case_study_template.md"
LEAD_PUBLIC = ROOT.parent.parent / "lead" / "public"
if not LEAD_PUBLIC.parent.exists():
    LEAD_PUBLIC = ROOT.parent / "lead" / "public"

SITE_BASE = os.environ.get("SITE_URL", "https://dentalmeet.agency").rstrip("/")
MEETINGS_THRESHOLD = 10  # legacy permission-request trigger
DEFAULT_INDUSTRY = "Marketing Agency"
CLOSED_RATE = 0.25
REVENUE_PER_CLOSE = 1500
DEFAULT_AVG_CLIENT_VALUE = 1500
DEFAULT_COST_PAID = 750

CASE_STUDY_SHEET_FIELDS = [
    "start_date",
    "emails_sent",
    "meetings_booked",
    "clients_acquired",
    "avg_client_value",
    "cost_paid_to_us",
    "team_size",
    "niche",
    "case_study_generated",
    "case_study_url",
    "client_testimonial",
    "testimonial_requested",
]

SAMPLE_CLIENT = {
    "client_email": "test@x.com",
    "agency_name": "Bright Smile Marketing",
    "owner_name": "Alex Rivera",
    "title": "Founder",
    "location": "Austin, TX",
    "region": "Texas",
    "team_size": "8",
    "niche": "dental marketing",
    "hours": "12",
    "old_meetings": "2",
    "leads_found": 240,
    "emails_sent": 285,
    "meetings_booked": 14,
    "clients_acquired": 3,
    "avg_client_value": 1800,
    "cost_paid_to_us": 750,
    "start_date": (date.today() - timedelta(days=45)).isoformat(),
    "client_testimonial": (
        "DentalMeet filled our calendar with qualified practice owners. "
        "We signed three new retainers in the first six weeks."
    ),
}


def _load() -> dict:
    if not CASE_STUDIES_PATH.exists():
        return {"drafts": [], "approved": [], "pending_permission": [], "published": []}
    try:
        data = json.loads(CASE_STUDIES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"drafts": [], "approved": [], "pending_permission": [], "published": []}
    for key in ("drafts", "approved", "pending_permission", "published"):
        data.setdefault(key, [])
    return data


def _save(data: dict) -> None:
    CASE_STUDIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    CASE_STUDIES_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return s.strip("-") or "agency"


def _month_bounds(ref: date | None = None) -> tuple[date, date]:
    ref = ref or date.today()
    start = ref.replace(day=1)
    if ref.month == 12:
        end = date(ref.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(ref.year, ref.month + 1, 1) - timedelta(days=1)
    return start, end


def _anonymize_location(location: str) -> str:
    loc = (location or "").strip()
    if not loc:
        return "the United States"
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return loc


def _format_location(location: str, *, named: bool) -> str:
    loc = (location or "").strip()
    if not loc:
        return "the United States"
    if named:
        return loc
    anon = _anonymize_location(loc)
    if anon.lower() in ("ca", "california"):
        return "California"
    if len(anon) <= 3 and anon.isalpha():
        return anon.upper()
    return anon


def _format_industry(*, named: bool, company: str = "") -> str:
    if named and company:
        return company
    return DEFAULT_INDUSTRY


def _ascii(text: str) -> str:
    return (text or "").encode("ascii", "replace").decode("ascii")


def _int(val: Any, default: int = 0) -> int:
    try:
        return int(float(str(val or "").replace(",", "").replace("$", "").strip() or default))
    except (TypeError, ValueError):
        return default


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(str(val or "").replace(",", "").replace("$", "").strip() or default)
    except (TypeError, ValueError):
        return default


def ensure_case_study_columns() -> dict[str, int]:
    """Ensure Clients sheet has case-study tracking columns."""
    from .client_tracker import TRACKER_HEADERS, ensure_tracker_columns

    for name in CASE_STUDY_SHEET_FIELDS:
        if name not in TRACKER_HEADERS:
            TRACKER_HEADERS.append(name)
    return ensure_tracker_columns()


def _client_row(email: str) -> tuple[dict[str, str], int | None, dict[str, int]]:
    """Return flat field map, 1-based row index, and column map."""
    from pipeline.to_sheets import get_all_values_safe, update_cell_safe

    cols = ensure_case_study_columns()
    from .client_tracker import _clients_ws

    ws = _clients_ws()
    values = get_all_values_safe(ws)
    email_l = email.strip().lower()
    email_i = cols.get("Email", 3)
    out: dict[str, str] = {}
    row_idx: int | None = None
    for i, row in enumerate(values[1:], start=2):
        cell = (row[email_i - 1] if len(row) >= email_i else "").strip().lower()
        if cell != email_l:
            continue
        row_idx = i
        for name, col in cols.items():
            out[name] = (row[col - 1] if len(row) >= col else "").strip()
        break
    return out, row_idx, cols


def _set_client_fields(email: str, updates: dict[str, str]) -> None:
    from pipeline.to_sheets import update_cell_safe

    from .client_tracker import _clients_ws

    row_data, row_idx, cols = _client_row(email)
    if not row_idx:
        return
    ws = _clients_ws()
    for key, val in updates.items():
        col = cols.get(key)
        if not col:
            continue
        update_cell_safe(ws, row_idx, col, str(val))


def record_campaign_start(client_email: str, start: date | None = None) -> None:
    """When a client's campaign starts → record start_date if empty."""
    email = client_email.strip().lower()
    try:
        row, row_idx, _ = _client_row(email)
        if not row_idx:
            return
        if (row.get("start_date") or "").strip():
            return
        _set_client_fields(email, {"start_date": (start or date.today()).isoformat()})
        print(f"case study start_date recorded for {email}")
    except Exception as e:
        print(f"record_campaign_start skipped: {e}")


def sync_client_metrics(client_email: str) -> dict:
    """Refresh emails_sent / meetings_booked / clients_acquired on the Clients sheet."""
    email = client_email.strip().lower()
    stats = collect_success_metrics(email)
    updates = {
        "emails_sent": str(stats.get("emails_sent") or 0),
        "meetings_booked": str(stats.get("meetings_booked") or 0),
        "clients_acquired": str(stats.get("clients_acquired") or 0),
    }
    if stats.get("start_date") and not (stats.get("_had_start_date")):
        updates["start_date"] = str(stats["start_date"])
    try:
        _set_client_fields(email, updates)
    except Exception as e:
        print(f"sync_client_metrics sheet update skipped: {e}")
    return stats


def collect_month_stats(client_email: str, *, meetings_override: int | None = None) -> dict:
    """Gather campaign metrics for the current calendar month (legacy)."""
    from .client_reports import get_client_profile

    email = client_email.strip().lower()
    profile = get_client_profile(email)
    start, end = _month_bounds()
    sent_path = ROOT / "logs" / f"{_safe(email)}_sent.csv"
    replies_path = ROOT / "logs" / f"{_safe(email)}_replies.csv"

    emails_sent = 0
    if sent_path.exists():
        with sent_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    d = date.fromisoformat((row.get("date") or "")[:10])
                except ValueError:
                    continue
                if start <= d <= end:
                    emails_sent += 1

    replies = 0
    if replies_path.exists():
        with replies_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    d = date.fromisoformat((row.get("date") or "")[:10])
                except ValueError:
                    continue
                if start <= d <= end:
                    replies += 1

    meetings_booked = meetings_override
    geo = profile.get("geo") or ""
    if meetings_booked is None:
        meetings_booked = 0
        try:
            row, _, cols = _client_row(email)
            meetings_booked = _int(row.get("meetings_this_month") or row.get("meetings_booked"))
            geo = row.get("client_geo") or row.get("Geo") or geo
        except Exception as e:
            print(f"meetings lookup skipped: {e}")

        if meetings_booked == 0:
            try:
                from .qualify_meetings import qualify_all_meetings

                q = qualify_all_meetings(email, period="month")
                meetings_booked = int(q.get("total_meetings") or q.get("qualified_meetings") or 0)
            except Exception:
                pass

    reply_rate = round(100.0 * replies / emails_sent, 1) if emails_sent else 0.0
    meeting_conversion = (
        round(100.0 * meetings_booked / emails_sent, 1) if emails_sent else 0.0
    )

    duration_days = 30
    try:
        row, _, _ = _client_row(email)
        for created_key in ("start_date", "Created", "Created Date", "Start Date"):
            raw = (row.get(created_key) or "").strip()[:10]
            if not raw:
                continue
            try:
                created = date.fromisoformat(raw)
                duration_days = max(1, (date.today() - created).days)
            except ValueError:
                pass
            break
    except Exception:
        pass

    closed = max(1, int(meetings_booked * CLOSED_RATE)) if meetings_booked else 0
    revenue = closed * REVENUE_PER_CLOSE

    return {
        "client_email": email,
        "client_name": profile.get("name") or "there",
        "company": profile.get("company") or "",
        "location": geo or profile.get("geo") or "",
        "emails_sent": emails_sent,
        "replies": replies,
        "meetings_booked": meetings_booked,
        "reply_rate_pct": reply_rate,
        "meeting_conversion_pct": meeting_conversion,
        "duration_days": duration_days,
        "closed_estimate": closed,
        "revenue_estimate": revenue,
    }


def collect_success_metrics(client_email: str) -> dict:
    """Full success metrics for case study generation (lifetime / sheet-backed)."""
    email = client_email.strip().lower()
    if email == SAMPLE_CLIENT["client_email"]:
        return _sample_metrics()

    from .client_reports import get_client_profile

    profile = get_client_profile(email)
    row: dict[str, str] = {}
    try:
        row, _, _ = _client_row(email)
    except Exception as e:
        print(f"client row lookup skipped: {e}")

    # Lifetime emails from sent log
    emails_sent = _int(row.get("emails_sent"))
    sent_path = ROOT / "logs" / f"{_safe(email)}_sent.csv"
    if sent_path.exists():
        with sent_path.open(newline="", encoding="utf-8") as f:
            emails_sent = max(emails_sent, sum(1 for _ in csv.DictReader(f)))

    meetings_booked = _int(row.get("meetings_booked") or row.get("Meetings") or row.get("meetings_this_month"))
    if meetings_booked == 0:
        try:
            from .qualify_meetings import qualify_all_meetings

            q = qualify_all_meetings(email, period="all")
            meetings_booked = int(q.get("total_meetings") or q.get("qualified_meetings") or 0)
        except Exception:
            pass

    clients_acquired = _int(row.get("clients_acquired"))
    if clients_acquired == 0 and meetings_booked:
        # Conservative estimate until manually tracked
        clients_acquired = max(0, int(meetings_booked * CLOSED_RATE))

    avg_client_value = _float(row.get("avg_client_value"), DEFAULT_AVG_CLIENT_VALUE)
    cost_paid = _float(
        row.get("cost_paid_to_us") or row.get("Total Paid") or row.get("Payment Amount"),
        DEFAULT_COST_PAID,
    )

    start_raw = (row.get("start_date") or row.get("Created") or row.get("Created Date") or "").strip()[:10]
    had_start = bool((row.get("start_date") or "").strip())
    start_d: date | None = None
    if start_raw:
        try:
            start_d = date.fromisoformat(start_raw)
        except ValueError:
            start_d = None
    if not start_d:
        start_d = date.today() - timedelta(days=30)

    days = max(1, (date.today() - start_d).days)
    leads_found = emails_sent or _int(row.get("leads_found"), meetings_booked * 20 or 200)

    location = (
        row.get("client_geo")
        or row.get("Geo")
        or profile.get("geo")
        or "the United States"
    )
    region = _anonymize_location(location)
    agency = (row.get("Company") or profile.get("company") or "Agency").strip()
    owner = (row.get("Name") or profile.get("name") or "there").strip()
    testimonial = (row.get("client_testimonial") or "").strip()

    revenue = int(clients_acquired * avg_client_value)
    roi = round(revenue / cost_paid, 1) if cost_paid > 0 else 0.0

    return {
        "client_email": email,
        "agency_name": agency,
        "owner_name": owner.split()[0] if owner else "there",
        "owner_full": owner,
        "title": (row.get("Title") or "Owner").strip() or "Owner",
        "location": location,
        "region": region,
        "team_size": (row.get("team_size") or "5").strip() or "5",
        "niche": (row.get("niche") or row.get("services") or "dental marketing").strip()
        or "dental marketing",
        "hours": (row.get("outreach_hours") or "10").strip() or "10",
        "old_meetings": (row.get("old_meetings") or "2").strip() or "2",
        "leads_found": leads_found,
        "emails_sent": emails_sent,
        "meetings_booked": meetings_booked,
        "clients_acquired": clients_acquired,
        "avg_client_value": avg_client_value,
        "cost_paid_to_us": cost_paid,
        "revenue_generated": revenue,
        "roi": roi,
        "start_date": start_d.isoformat(),
        "days": days,
        "timeframe": _timeframe_label(days),
        "client_testimonial": testimonial,
        "case_study_generated": (row.get("case_study_generated") or "").lower() in ("true", "1", "yes"),
        "case_study_url": row.get("case_study_url") or "",
        "testimonial_requested": (row.get("testimonial_requested") or "").lower()
        in ("true", "1", "yes"),
        "_had_start_date": had_start,
    }


def _sample_metrics() -> dict:
    s = dict(SAMPLE_CLIENT)
    start = date.fromisoformat(s["start_date"])
    days = max(1, (date.today() - start).days)
    revenue = int(s["clients_acquired"] * s["avg_client_value"])
    cost = float(s["cost_paid_to_us"])
    return {
        **s,
        "owner_full": s["owner_name"],
        "revenue_generated": revenue,
        "roi": round(revenue / cost, 1) if cost else 0,
        "days": days,
        "timeframe": _timeframe_label(days),
        "case_study_generated": False,
        "case_study_url": "",
        "testimonial_requested": False,
        "_had_start_date": True,
    }


def _timeframe_label(days: int) -> str:
    if days <= 35:
        return f"{days} days"
    months = max(1, round(days / 30))
    return f"{months} month" if months == 1 else f"{months} months"


def load_template() -> str:
    if TEMPLATE_PATH.exists():
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        "# How {agency_name} Added {clients_acquired} New Dental Clients in {timeframe}\n"
    )


def fill_template(metrics: dict) -> str:
    tpl = load_template()
    testimonial = (metrics.get("client_testimonial") or "").strip() or (
        "Working with DentalMeet has been a game changer for our pipeline."
    )
    values = {
        "agency_name": metrics.get("agency_name") or "Agency",
        "clients_acquired": metrics.get("clients_acquired") or 0,
        "timeframe": metrics.get("timeframe") or "30 days",
        "team_size": metrics.get("team_size") or "5",
        "location": metrics.get("location") or "the United States",
        "niche": metrics.get("niche") or "dental marketing",
        "hours": metrics.get("hours") or "10",
        "old_meetings": metrics.get("old_meetings") or "2",
        "leads_found": metrics.get("leads_found") or metrics.get("emails_sent") or 0,
        "region": metrics.get("region") or _anonymize_location(metrics.get("location") or ""),
        "meetings_booked": metrics.get("meetings_booked") or 0,
        "revenue_generated": f"{int(metrics.get('revenue_generated') or 0):,}",
        "roi": metrics.get("roi") or 0,
        "days": metrics.get("days") or 30,
        "testimonial": testimonial,
        "owner_name": metrics.get("owner_full") or metrics.get("owner_name") or "Owner",
        "title": metrics.get("title") or "Owner",
    }

    def repl(match: re.Match) -> str:
        key = match.group(1)
        return str(values.get(key, match.group(0)))

    return re.sub(r"\{(\w+)\}", repl, tpl)


def build_social_snippets(metrics: dict, url: str) -> dict[str, str]:
    agency = metrics.get("agency_name") or "a dental marketing agency"
    n = metrics.get("clients_acquired") or 0
    meetings = metrics.get("meetings_booked") or 0
    roi = metrics.get("roi") or 0
    linkedin = (
        f"Case study: {agency} added {n} new dental clients "
        f"({meetings} qualified meetings, {roi}x ROI).\n\n"
        f"Full story → {url}\n\n"
        f"#DentalMarketing #B2BOutreach #LeadGeneration"
    )
    twitter = (
        f"How {agency} added {n} new dental clients "
        f"({meetings} meetings, {roi}x ROI): {url}"
    )
    return {"linkedin": linkedin, "twitter": twitter, "x": twitter}


def markdown_to_simple_html(md: str, *, title: str, cta_url: str) -> str:
    lines = md.splitlines()
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8"/>',
        f"<title>{_ascii(title)}</title>",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>",
        "<style>",
        "body{font-family:Georgia,serif;max-width:720px;margin:2rem auto;padding:0 1.25rem;"
        "line-height:1.6;color:#1a1a1a}",
        "h1{font-size:1.75rem;line-height:1.25}",
        "h2{margin-top:2rem;font-size:1.25rem}",
        "ul{padding-left:1.25rem}",
        "a.cta{display:inline-block;margin-top:1.5rem;padding:0.75rem 1.25rem;"
        "background:#0d9488;color:#fff;text-decoration:none;border-radius:8px;font-weight:600}",
        "</style></head><body>",
    ]
    in_ul = False
    for line in lines:
        s = line.strip()
        if s.startswith("# "):
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            parts.append(f"<h1>{_escape_html(s[2:])}</h1>")
        elif s.startswith("## "):
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            parts.append(f"<h2>{_escape_html(s[3:])}</h2>")
        elif s.startswith("- "):
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            item = s[2:]
            item = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item)
            parts.append(f"<li>{item}</li>")
        elif s.startswith("[Get Started"):
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            parts.append(f'<a class="cta" href="{cta_url}">Get Started</a>')
        elif s:
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _escape_html(s))
            if s.startswith('"') or s.startswith("—"):
                parts.append(f"<p><em>{text}</em></p>")
            else:
                parts.append(f"<p>{text}</p>")
    if in_ul:
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _escape_html(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_case_study_pdf(slug: str, metrics: dict, markdown: str) -> Path:
    out_dir = CASE_STUDIES_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.pdf"
    try:
        from fpdf import FPDF
    except ImportError:
        fallback = out_dir / f"{slug}.pdf.md"
        fallback.write_text(markdown, encoding="utf-8")
        print(f"fpdf2 missing — wrote markdown instead: {fallback}")
        return fallback

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_margins(14, 14, 14)
    pdf.set_font("Helvetica", "B", 16)
    title = f"How {metrics.get('agency_name')} Added {metrics.get('clients_acquired')} Clients"
    pdf.multi_cell(0, 8, _ascii(title))
    pdf.ln(4)
    pdf.set_font("Helvetica", size=11)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    for line in markdown.splitlines():
        s = line.strip()
        if not s or s.startswith("# "):
            continue
        if s.startswith("## "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 12)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(usable, 7, _ascii(s[3:]))
            pdf.set_font("Helvetica", size=11)
            continue
        if s.startswith("- "):
            s = "* " + re.sub(r"\*\*(.+?)\*\*", r"\1", s[2:])
        elif s.startswith("["):
            s = "Get Started: https://dentalmeet.agency/pilot"
        else:
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        text = _ascii(s)
        if not text.strip():
            continue
        pdf.set_x(pdf.l_margin)
        try:
            pdf.multi_cell(usable, 6, text)
        except Exception:
            pdf.multi_cell(usable, 6, text[:120])
    try:
        pdf.output(str(path))
    except Exception as e:
        fallback = out_dir / f"{slug}.pdf.md"
        fallback.write_text(markdown, encoding="utf-8")
        print(f"PDF write failed ({e}) — wrote markdown: {fallback}")
        return fallback
    return path


def generate_case_study(
    client_email: str,
    *,
    force: bool = False,
    publish: bool = True,
    send_testimonial: bool = True,
) -> dict:
    """
    Generate case study outputs for a client.
    Trigger condition: clients_acquired > 0 (or sample test@x.com).
    """
    email = client_email.strip().lower()
    metrics = collect_success_metrics(email)
    acquired = _int(metrics.get("clients_acquired"))
    if email != SAMPLE_CLIENT["client_email"] and acquired <= 0 and not force:
        return {
            "ok": False,
            "error": "clients_acquired must be > 0 (use --force to override)",
            "metrics": metrics,
        }

    agency = metrics.get("agency_name") or "agency"
    slug = _slugify(agency)
    url = f"{SITE_BASE}/case-studies/{slug}"
    markdown = fill_template(metrics)
    social = build_social_snippets(metrics, url)

    CASE_STUDIES_DIR.mkdir(parents=True, exist_ok=True)
    md_path = CASE_STUDIES_DIR / f"{slug}.md"
    md_path.write_text(markdown + "\n", encoding="utf-8")

    html = markdown_to_simple_html(
        markdown,
        title=f"{agency} Case Study | DentalMeet",
        cta_url=f"{SITE_BASE}/pilot",
    )
    html_dir = CASE_STUDIES_DIR / slug
    html_dir.mkdir(parents=True, exist_ok=True)
    local_html = html_dir / "index.html"
    local_html.write_text(html, encoding="utf-8")

    if LEAD_PUBLIC.parent.exists():
        site_dir = LEAD_PUBLIC / "case-studies" / slug
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "index.html").write_text(html, encoding="utf-8")

    pdf_path = write_case_study_pdf(slug, metrics, markdown)
    social_path = CASE_STUDIES_DIR / slug / "social.json"
    social_path.write_text(json.dumps(social, indent=2) + "\n", encoding="utf-8")

    record = {
        "id": str(uuid.uuid4())[:8],
        "slug": slug,
        "client_email": email,
        "agency_name": agency,
        "owner_name": metrics.get("owner_full") or metrics.get("owner_name"),
        "title": metrics.get("title"),
        "location": metrics.get("location"),
        "industry": metrics.get("niche") or DEFAULT_INDUSTRY,
        "team_size": metrics.get("team_size"),
        "niche": metrics.get("niche"),
        "emails_sent": metrics.get("emails_sent"),
        "leads_found": metrics.get("leads_found"),
        "meetings_booked": metrics.get("meetings_booked"),
        "clients_acquired": acquired,
        "revenue_generated": f"${int(metrics.get('revenue_generated') or 0):,}",
        "revenue_generated_raw": metrics.get("revenue_generated"),
        "roi": metrics.get("roi"),
        "days": metrics.get("days"),
        "duration": metrics.get("timeframe"),
        "timeframe": metrics.get("timeframe"),
        "quote": metrics.get("client_testimonial") or None,
        "markdown": markdown,
        "url": url,
        "md_path": str(md_path),
        "pdf_path": str(pdf_path),
        "social": social,
        "approved": True,
        "show_name": True,
        "published": bool(publish),
        "key_metric": f"{acquired} new clients",
        "prose": _prose_from_metrics(metrics),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "published" if publish else "draft",
    }

    data = _load()
    # Replace existing published/approved entry for same slug or email
    for bucket in ("published", "approved", "drafts"):
        data[bucket] = [
            x
            for x in (data.get(bucket) or [])
            if (x.get("slug") != slug and (x.get("client_email") or "").lower() != email)
        ]
    if publish:
        data.setdefault("published", []).append(record)
        data.setdefault("approved", []).append(record)
    else:
        data.setdefault("drafts", []).append(record)
    _save(data)

    if publish:
        export_to_site(data)

    sheet_updates = {
        "case_study_generated": "true",
        "case_study_url": url,
        "emails_sent": str(metrics.get("emails_sent") or 0),
        "meetings_booked": str(metrics.get("meetings_booked") or 0),
        "clients_acquired": str(acquired),
    }
    if metrics.get("client_testimonial"):
        sheet_updates["client_testimonial"] = metrics["client_testimonial"]
    if metrics.get("start_date"):
        sheet_updates.setdefault("start_date", str(metrics["start_date"]))
    if email != SAMPLE_CLIENT["client_email"]:
        try:
            _set_client_fields(email, sheet_updates)
        except Exception as e:
            print(f"sheet update skipped: {e}")

    testimonial_result = None
    if send_testimonial and acquired >= 2 and not metrics.get("client_testimonial"):
        if not metrics.get("testimonial_requested"):
            testimonial_result = request_testimonial(email, metrics)

    print(f"case study generated: {md_path}")
    print(f"url: {url}")
    return {
        "ok": True,
        "slug": slug,
        "url": url,
        "md_path": str(md_path),
        "pdf_path": str(pdf_path),
        "social": social,
        "record": record,
        "testimonial": testimonial_result,
    }


def _prose_from_metrics(metrics: dict) -> str:
    return (
        f"{metrics.get('agency_name')}, a {metrics.get('team_size')}-person agency in "
        f"{metrics.get('location')}, partnered with DentalMeet and signed "
        f"{metrics.get('clients_acquired')} new dental clients in {metrics.get('timeframe')} "
        f"after {metrics.get('meetings_booked')} qualified meetings."
    )


def request_testimonial(client_email: str, metrics: dict | None = None) -> dict:
    """Email client asking for a short testimonial when clients_acquired >= 2."""
    from pipeline.email_sender import send_email

    email = client_email.strip().lower()
    metrics = metrics or collect_success_metrics(email)
    if _int(metrics.get("clients_acquired")) < 2:
        return {"ok": False, "error": "clients_acquired < 2"}
    if metrics.get("client_testimonial"):
        return {"ok": False, "error": "testimonial already on file"}

    name = (metrics.get("owner_name") or "there").split()[0]
    subject = "Quick favor — a short testimonial?"
    body = (
        f"Hi {name},\n\n"
        f"Congrats on your new clients! Would you mind sharing a "
        f"1-2 sentence testimonial? It helps us show others what's "
        f"possible.\n\n"
        f"Just reply to this email with whatever feels natural.\n\n"
        f"Thanks!\n"
        f"Vladislav\n"
        f"DentalMeet\n"
    )
    ok = False
    try:
        ok = bool(send_email(email, subject, body))
    except Exception as e:
        print(f"testimonial email failed: {e}")
    if ok and email != SAMPLE_CLIENT["client_email"]:
        try:
            _set_client_fields(email, {"testimonial_requested": "true"})
        except Exception:
            pass
    print(f"testimonial request → {email} ok={ok}")
    return {"ok": ok, "email": email}


def save_testimonial(client_email: str, quote: str) -> dict:
    """Persist client testimonial and regenerate case study if present."""
    email = client_email.strip().lower()
    quote = (quote or "").strip()
    if not quote:
        return {"ok": False, "error": "empty quote"}
    try:
        _set_client_fields(email, {"client_testimonial": quote})
    except Exception as e:
        print(f"save_testimonial sheet skipped: {e}")
    data = _load()
    for bucket in ("published", "approved", "drafts"):
        for item in data.get(bucket) or []:
            if (item.get("client_email") or "").lower() == email:
                item["quote"] = quote
    _save(data)
    return generate_case_study(email, force=True, send_testimonial=False)


def publish_case_study(slug: str) -> dict:
    """Mark a generated case study as published and export to site."""
    slug = _slugify(slug)
    data = _load()
    found = None
    for bucket in ("drafts", "published", "approved", "pending_permission"):
        for i, item in enumerate(list(data.get(bucket) or [])):
            if item.get("slug") == slug or _slugify(item.get("agency_name") or "") == slug:
                found = item
                if bucket == "drafts":
                    data["drafts"].pop(i)
                break
        if found:
            break
    if not found:
        # Try regenerating from markdown file
        md = CASE_STUDIES_DIR / f"{slug}.md"
        if not md.exists():
            return {"ok": False, "error": f"case study not found: {slug}"}
        found = {
            "id": str(uuid.uuid4())[:8],
            "slug": slug,
            "agency_name": slug.replace("-", " ").title(),
            "markdown": md.read_text(encoding="utf-8"),
            "url": f"{SITE_BASE}/case-studies/{slug}",
            "key_metric": "Client results",
            "prose": md.read_text(encoding="utf-8")[:400],
        }

    found["published"] = True
    found["approved"] = True
    found["status"] = "published"
    found["url"] = found.get("url") or f"{SITE_BASE}/case-studies/{slug}"
    data["published"] = [x for x in (data.get("published") or []) if x.get("slug") != slug]
    data["approved"] = [x for x in (data.get("approved") or []) if x.get("slug") != slug]
    data["published"].append(found)
    data["approved"].append(found)
    _save(data)
    export_to_site(data)

    if found.get("client_email") and found["client_email"] != SAMPLE_CLIENT["client_email"]:
        try:
            _set_client_fields(
                found["client_email"],
                {"case_study_generated": "true", "case_study_url": found["url"]},
            )
        except Exception:
            pass
    return {"ok": True, "slug": slug, "url": found["url"]}


def list_case_studies() -> dict:
    data = _load()
    published = data.get("published") or data.get("approved") or []
    files = sorted(p.name for p in CASE_STUDIES_DIR.glob("*.md")) if CASE_STUDIES_DIR.exists() else []
    return {
        "updated_at": data.get("updated_at"),
        "published": [
            {
                "slug": cs.get("slug"),
                "agency_name": cs.get("agency_name") or cs.get("company"),
                "clients_acquired": cs.get("clients_acquired"),
                "meetings_booked": cs.get("meetings_booked"),
                "url": cs.get("url"),
                "client_email": cs.get("client_email"),
            }
            for cs in published
        ],
        "pending_permission": data.get("pending_permission") or [],
        "drafts": data.get("drafts") or [],
        "approved": data.get("approved") or [],
        "markdown_files": files,
    }


def next_case_study_blurb() -> str:
    """Rotate through published case studies for A1–A10 injection."""
    data = _load()
    studies = [cs for cs in (data.get("published") or data.get("approved") or []) if cs.get("url")]
    if not studies:
        return ""
    idx_path = ROOT / "data" / "case_study_rotation.json"
    idx = 0
    try:
        if idx_path.exists():
            idx = int(json.loads(idx_path.read_text(encoding="utf-8")).get("index") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        idx = 0
    cs = studies[idx % len(studies)]
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps({"index": (idx + 1) % len(studies)}) + "\n", encoding="utf-8")
    agency = cs.get("agency_name") or cs.get("company") or "a similar agency"
    n = cs.get("clients_acquired") or cs.get("meetings_booked") or "several"
    url = cs.get("url") or f"{SITE_BASE}/case-studies"
    return f"See how {agency} got {n} new clients: {url}"


def weekly_auto_generate(*, dry_run: bool = False) -> dict:
    """
    Weekly cron: clients with clients_acquired > 0 and no case_study_generated
    → auto-generate.
    """
    from delivery.weekly_report import active_clients

    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    emails = list(active_clients())
    # Always allow scanning sheet directly if active list empty
    try:
        ensure_case_study_columns()
        from pipeline.to_sheets import get_all_values_safe

        from .client_tracker import _clients_ws

        ws = _clients_ws()
        values = get_all_values_safe(ws)
        cols = {h.strip(): i for i, h in enumerate(values[0])} if values else {}
        email_i = cols.get("Email", 2)
        for row in values[1:]:
            if len(row) <= email_i:
                continue
            em = (row[email_i] or "").strip().lower()
            if em and em not in emails:
                emails.append(em)
    except Exception as e:
        print(f"weekly sheet scan skipped: {e}")

    for email in emails:
        try:
            metrics = collect_success_metrics(email)
            if _int(metrics.get("clients_acquired")) <= 0:
                continue
            if metrics.get("case_study_generated") and not dry_run:
                skipped.append(email)
                continue
            if dry_run:
                print(f"[dry-run] would generate case study for {email}")
                created.append(email)
                continue
            result = generate_case_study(email, send_testimonial=True)
            if result.get("ok"):
                created.append(email)
            else:
                errors.append(f"{email}: {result.get('error')}")
        except Exception as e:
            errors.append(f"{email}: {e}")
            print(f"weekly generate failed for {email}: {e}")

    return {"created": created, "skipped": skipped, "errors": errors, "count": len(created)}


def build_case_study_draft(
    client_email: str,
    *,
    meetings_override: int | None = None,
    stats: dict | None = None,
) -> dict:
    stats = stats or collect_month_stats(client_email, meetings_override=meetings_override)
    location = stats.get("location") or ""
    meetings = int(stats.get("meetings_booked") or 0)
    emails = int(stats.get("emails_sent") or 0)
    duration_days = int(stats.get("duration_days") or 30)
    reply_rate = stats.get("reply_rate_pct", 0)
    conversion = stats.get("meeting_conversion_pct", 0)
    closed = int(stats.get("closed_estimate") or 0)
    revenue = int(stats.get("revenue_estimate") or 0)

    draft = {
        "id": str(uuid.uuid4())[:8],
        "client_email": stats["client_email"],
        "client_industry": DEFAULT_INDUSTRY,
        "client_location": location or "Los Angeles, CA",
        "duration": f"{duration_days} days",
        "meetings_booked": meetings,
        "emails_sent": emails,
        "reply_rate": f"{reply_rate}%",
        "meeting_conversion": f"{conversion}%",
        "revenue_generated": f"${revenue:,} (client's estimate)",
        "closed_deals": closed,
        "quote": None,
        "approved": False,
        "permission_requested": False,
        "permission_granted": False,
        "show_name": False,
        "show_logo": False,
        "client_name": stats.get("client_name") or "",
        "company": stats.get("company") or "",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    draft["prose"] = render_prose(draft, named=False)
    draft["prose_named"] = render_prose(draft, named=True)
    return draft


def render_prose(draft: dict, *, named: bool = False) -> str:
    location = _format_location(draft.get("client_location") or "", named=named)
    industry = _format_industry(named=named, company=draft.get("company") or "")
    meetings = draft.get("meetings_booked", 0)
    emails = draft.get("emails_sent", 0)
    duration = draft.get("duration") or "30 days"
    conversion = draft.get("meeting_conversion") or "0%"
    revenue = draft.get("revenue_generated") or ""
    closed = draft.get("closed_deals") or max(1, int(meetings * CLOSED_RATE))

    if named and draft.get("company"):
        opener = f"{draft['company']}, a {location}-based marketing agency, partnered with DentalMeet"
    elif named:
        opener = f"A {location}-based marketing agency partnered with DentalMeet"
    else:
        opener = f"A marketing agency in {location} partnered with DentalMeet"

    body = (
        f"{opener} to fill their calendar with dental practice clients. "
        f"In just {duration}, we sent {emails} personalized emails and booked "
        f"{meetings} qualified meetings — a {conversion} conversion rate from "
        f"email to meeting. The agency closed {closed} of these meetings into "
        f"paying clients, generating an estimated {revenue}."
    )
    quote = draft.get("quote")
    if quote:
        body += f'\n\n"{quote}"'
    return body


def send_permission_request(draft: dict) -> bool:
    from pipeline.email_sender import send_email

    email = draft.get("client_email") or ""
    name = (draft.get("client_name") or "there").split()[0]
    meetings = draft.get("meetings_booked", 0)
    subject = "Can we feature your results?"
    body = (
        f"Hi {name},\n\n"
        f"Your campaign has been doing great! You've booked {meetings} meetings "
        f"this month.\n\n"
        f"We'd love to feature your results as a case study on our website "
        f"(anonymized or with your name, your choice).\n\n"
        f"Would you be open to this? Just reply \"yes\" and we'll send you "
        f"the draft to approve.\n\n"
        f"Thanks!\n"
        f"Vladislav\n"
    )
    ok = send_email(email, subject, body)
    if ok:
        print(f"permission request → {email}")
    return ok


def _already_tracked(data: dict, client_email: str) -> bool:
    email = client_email.strip().lower()
    for bucket in ("drafts", "approved", "pending_permission", "published"):
        for item in data.get(bucket) or []:
            if (item.get("client_email") or "").lower() == email:
                if item.get("month") == date.today().strftime("%Y-%m"):
                    return True
    return False


def check_triggers(*, dry_run: bool = False) -> dict:
    """
    Scan clients:
    1) clients_acquired > 0 without case study → generate
    2) legacy: 10+ meetings this month → permission request
    """
    weekly = weekly_auto_generate(dry_run=dry_run)

    from delivery.weekly_report import active_clients

    data = _load()
    created: list[str] = []
    month_key = date.today().strftime("%Y-%m")

    for email in active_clients():
        if _already_tracked(data, email):
            continue
        stats = collect_month_stats(email)
        if stats["meetings_booked"] < MEETINGS_THRESHOLD:
            continue
        draft = build_case_study_draft(email, stats=stats)
        draft["month"] = month_key
        draft["status"] = "pending_permission"
        if dry_run:
            print(f"[dry-run] would request permission for {email}: {draft['meetings_booked']} meetings")
            created.append(email)
            continue
        data.setdefault("pending_permission", []).append(draft)
        send_permission_request(draft)
        draft["permission_requested"] = True
        created.append(email)
        print(f"case study draft created for {email} ({draft['meetings_booked']} meetings)")

    if not dry_run and created:
        _save(data)
    return {
        "auto_generated": weekly,
        "permission_requests": created,
        "count": weekly.get("count", 0) + len(created),
    }


def approve_case_study(
    case_id: str,
    *,
    quote: str | None = None,
    show_name: bool = False,
    show_logo: bool = False,
) -> dict:
    """Move a pending/draft case study to approved and publish."""
    data = _load()
    found = None

    for bucket in ("pending_permission", "drafts"):
        for i, item in enumerate(data.get(bucket) or []):
            if item.get("id") == case_id or item.get("client_email", "").lower() == case_id.lower():
                found = data[bucket].pop(i)
                break
        if found:
            break

    if not found:
        return {"ok": False, "error": f"case study not found: {case_id}"}

    found["approved"] = True
    found["permission_granted"] = True
    found["show_name"] = show_name
    found["show_logo"] = show_logo
    if quote:
        found["quote"] = quote.strip()
    found["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found["status"] = "approved"
    if not found.get("slug"):
        found["slug"] = _slugify(found.get("company") or found.get("agency_name") or found["id"])
    found["url"] = found.get("url") or f"{SITE_BASE}/case-studies/{found['slug']}"

    named = show_name
    pub = {
        "id": found["id"],
        "slug": found["slug"],
        "url": found["url"],
        "industry": _format_industry(named=named, company=found.get("company") or ""),
        "location": _format_location(found.get("client_location") or "", named=named),
        "duration": found.get("duration"),
        "meetings_booked": found.get("meetings_booked"),
        "emails_sent": found.get("emails_sent"),
        "reply_rate": found.get("reply_rate"),
        "meeting_conversion": found.get("meeting_conversion"),
        "revenue_generated": found.get("revenue_generated"),
        "quote": found.get("quote"),
        "prose": render_prose(found, named=named),
        "show_name": show_name,
        "show_logo": show_logo,
        "company": found.get("company") if show_name else None,
        "agency_name": found.get("company") if show_name else None,
        "key_metric": f"{found.get('meetings_booked')} meetings booked",
        "published": True,
    }
    merged = {**found, **pub}
    data.setdefault("approved", []).append(merged)
    data.setdefault("published", []).append(merged)
    _save(data)
    export_to_site(data)
    return {"ok": True, "id": found["id"], "published": pub}


def send_draft_for_review(case_id: str) -> dict:
    """Email client the draft prose after they reply yes."""
    from pipeline.email_sender import send_email

    data = _load()
    draft = None
    for bucket in ("pending_permission", "drafts"):
        for item in data.get(bucket) or []:
            if item.get("id") == case_id:
                draft = item
                break
    if not draft:
        return {"ok": False, "error": "draft not found"}

    email = draft.get("client_email") or ""
    name = (draft.get("client_name") or "there").split()[0]
    body = (
        f"Hi {name},\n\n"
        f"Thanks for agreeing! Here's the draft case study for your review:\n\n"
        f"---\n{draft.get('prose') or render_prose(draft, named=False)}\n---\n\n"
        f"Reply with any edits, a quote we can include, and whether you'd like "
        f"your company name shown or prefer to stay anonymous.\n\n"
        f"— Vladislav\nDentalMeet\n"
    )
    ok = send_email(email, "Your case study draft for review", body)
    if ok:
        draft["status"] = "awaiting_review"
        _save(data)
    return {"ok": ok, "email": email}


def site_payload(data: dict | None = None) -> dict:
    data = data or _load()
    approved = data.get("published") or data.get("approved") or []
    highlights = []
    for cs in approved[:6]:
        highlights.append(
            {
                "id": cs.get("id"),
                "slug": cs.get("slug"),
                "industry": cs.get("industry") or cs.get("client_industry") or cs.get("niche"),
                "location": cs.get("location")
                or _anonymize_location(cs.get("client_location") or ""),
                "key_metric": cs.get("key_metric")
                or f"{cs.get('clients_acquired') or cs.get('meetings_booked')} results",
                "quote": cs.get("quote"),
                "prose": cs.get("prose") or render_prose(cs, named=cs.get("show_name", False)),
                "url": cs.get("url"),
                "clients_acquired": cs.get("clients_acquired"),
                "markdown": cs.get("markdown"),
            }
        )

    meetings_vals = [int(cs.get("meetings_booked") or 0) for cs in approved if cs.get("meetings_booked")]
    avg_meetings = round(sum(meetings_vals) / len(meetings_vals), 1) if meetings_vals else 8
    agency_count = max(len(approved), 5)

    return {
        "updated_at": data.get("updated_at"),
        "social_proof": {
            "agencies_count": agency_count,
            "avg_meetings_per_month": avg_meetings,
            "headline": f"Trusted by {agency_count}+ marketing agencies",
            "subheadline": f"Average {avg_meetings} meetings per client per month",
        },
        "case_studies": [
            {
                "id": cs.get("id"),
                "slug": cs.get("slug"),
                "industry": cs.get("industry") or cs.get("client_industry") or cs.get("niche"),
                "location": cs.get("location")
                or _anonymize_location(cs.get("client_location") or ""),
                "duration": cs.get("duration") or cs.get("timeframe"),
                "meetings_booked": cs.get("meetings_booked"),
                "emails_sent": cs.get("emails_sent") or cs.get("leads_found"),
                "clients_acquired": cs.get("clients_acquired"),
                "reply_rate": cs.get("reply_rate"),
                "meeting_conversion": cs.get("meeting_conversion"),
                "revenue_generated": cs.get("revenue_generated"),
                "roi": cs.get("roi"),
                "quote": cs.get("quote"),
                "prose": cs.get("prose") or render_prose(cs, named=cs.get("show_name", False)),
                "company": (
                    (cs.get("company") or cs.get("agency_name"))
                    if cs.get("show_name", True)
                    else None
                ),
                "agency_name": cs.get("agency_name") or cs.get("company"),
                "key_metric": cs.get("key_metric")
                or f"{cs.get('clients_acquired') or cs.get('meetings_booked')} results",
                "url": cs.get("url")
                or (f"{SITE_BASE}/case-studies/{cs['slug']}" if cs.get("slug") else None),
                "markdown": cs.get("markdown"),
                "owner_name": cs.get("owner_name"),
                "title": cs.get("title"),
            }
            for cs in approved
        ],
        "highlights": highlights,
    }


def export_to_site(data: dict | None = None) -> None:
    payload = site_payload(data)
    targets = [CASE_STUDIES_PATH.parent / "case_studies_site.json"]
    if LEAD_PUBLIC.parent.exists():
        targets.append(LEAD_PUBLIC / "case_studies.json")
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"exported {len(payload['case_studies'])} case studies to site")


def simulate_case_study(
    client_email: str = "demo@agency.com",
    *,
    meetings: int = 12,
    location: str = "Los Angeles, CA",
) -> dict:
    """Test: client with 10+ meetings → generates draft."""
    stats = {
        "client_email": client_email.strip().lower(),
        "client_name": "Alex",
        "company": "Bright Smile Marketing",
        "location": location,
        "emails_sent": 285,
        "replies": 24,
        "meetings_booked": meetings,
        "reply_rate_pct": round(100 * 24 / 285, 1),
        "meeting_conversion_pct": round(100 * meetings / 285, 1),
        "duration_days": 30,
        "closed_estimate": 3,
        "revenue_estimate": 4500,
    }
    draft = build_case_study_draft(client_email, stats=stats)
    draft["month"] = date.today().strftime("%Y-%m")
    draft["status"] = "simulated"

    data = _load()
    if not _already_tracked(data, client_email):
        data.setdefault("drafts", []).append(draft)
        _save(data)

    return {
        "ok": True,
        "triggered": meetings >= MEETINGS_THRESHOLD,
        "draft": draft,
        "prose": draft["prose"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Case study generation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Show all case studies")

    p_gen = sub.add_parser("generate", help="Generate case study for a client")
    p_gen.add_argument("--client", required=True, help="Client email")
    p_gen.add_argument("--force", action="store_true", help="Generate even if clients_acquired=0")
    p_gen.add_argument("--no-publish", action="store_true")
    p_gen.add_argument("--no-testimonial", action="store_true")

    p_pub = sub.add_parser("publish", help="Publish case study by slug")
    p_pub.add_argument("--slug", required=True)

    sub.add_parser("check", help="Check clients and auto-generate / request permission")
    p_weekly = sub.add_parser("weekly", help="Weekly auto-generate for clients_acquired > 0")
    p_weekly.add_argument("--dry-run", action="store_true")

    p_check = sub.add_parser("trigger", help="Alias for check")
    p_check.add_argument("--dry-run", action="store_true")

    p_sim = sub.add_parser("simulate", help="Simulate case study for test client")
    p_sim.add_argument("--email", default="demo@agency.com")
    p_sim.add_argument("--meetings", type=int, default=12)
    p_sim.add_argument("--location", default="Los Angeles, CA")

    p_ap = sub.add_parser("approve", help="Approve and publish a case study")
    p_ap.add_argument("case_id")
    p_ap.add_argument("--quote", default="")
    p_ap.add_argument("--show-name", action="store_true")
    p_ap.add_argument("--show-logo", action="store_true")

    p_rev = sub.add_parser("send-draft", help="Email draft to client for review")
    p_rev.add_argument("case_id")

    p_test = sub.add_parser("request-testimonial", help="Send testimonial request email")
    p_test.add_argument("--client", required=True)

    p_save = sub.add_parser("save-testimonial", help="Save testimonial quote for client")
    p_save.add_argument("--client", required=True)
    p_save.add_argument("--quote", required=True)

    sub.add_parser("export", help="Export approved case studies to website JSON")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        print(json.dumps(list_case_studies(), indent=2))
        return 0
    if args.cmd == "generate":
        result = generate_case_study(
            args.client,
            force=args.force,
            publish=not args.no_publish,
            send_testimonial=not args.no_testimonial,
        )
        # Compact print for readability
        out = {k: v for k, v in result.items() if k != "record"}
        if result.get("record"):
            out["slug"] = result["record"].get("slug")
            out["md_preview"] = (result["record"].get("markdown") or "")[:500]
        print(json.dumps(out, indent=2))
        return 0 if result.get("ok") else 1
    if args.cmd == "publish":
        print(json.dumps(publish_case_study(args.slug), indent=2))
        return 0
    if args.cmd == "weekly":
        print(json.dumps(weekly_auto_generate(dry_run=args.dry_run), indent=2))
        return 0
    if args.cmd in ("check", "trigger"):
        dry = getattr(args, "dry_run", False)
        print(json.dumps(check_triggers(dry_run=dry), indent=2))
        return 0
    if args.cmd == "simulate":
        print(
            json.dumps(
                simulate_case_study(args.email, meetings=args.meetings, location=args.location),
                indent=2,
            )
        )
        return 0
    if args.cmd == "approve":
        print(
            json.dumps(
                approve_case_study(
                    args.case_id,
                    quote=args.quote or None,
                    show_name=args.show_name,
                    show_logo=args.show_logo,
                ),
                indent=2,
            )
        )
        return 0
    if args.cmd == "send-draft":
        print(json.dumps(send_draft_for_review(args.case_id), indent=2))
        return 0
    if args.cmd == "request-testimonial":
        print(json.dumps(request_testimonial(args.client), indent=2))
        return 0
    if args.cmd == "save-testimonial":
        print(json.dumps(save_testimonial(args.client, args.quote), indent=2))
        return 0
    if args.cmd == "export":
        export_to_site()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
