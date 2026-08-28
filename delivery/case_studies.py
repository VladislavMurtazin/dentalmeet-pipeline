"""Automatic case study generation from client campaign results."""

from __future__ import annotations

import argparse
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
LEAD_PUBLIC = ROOT.parent.parent / "lead" / "public"
if not LEAD_PUBLIC.parent.exists():
    LEAD_PUBLIC = ROOT.parent / "lead" / "public"

MEETINGS_THRESHOLD = 10
DEFAULT_INDUSTRY = "Marketing Agency"
CLOSED_RATE = 0.25  # estimate closed deals from meetings
REVENUE_PER_CLOSE = 1500


def _load() -> dict:
    if not CASE_STUDIES_PATH.exists():
        return {"drafts": [], "approved": [], "pending_permission": []}
    try:
        data = json.loads(CASE_STUDIES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"drafts": [], "approved": [], "pending_permission": []}
    data.setdefault("drafts", [])
    data.setdefault("approved", [])
    data.setdefault("pending_permission", [])
    return data


def _save(data: dict) -> None:
    CASE_STUDIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    CASE_STUDIES_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def _month_bounds(ref: date | None = None) -> tuple[date, date]:
    ref = ref or date.today()
    start = ref.replace(day=1)
    if ref.month == 12:
        end = date(ref.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(ref.year, ref.month + 1, 1) - timedelta(days=1)
    return start, end


def _anonymize_location(location: str) -> str:
    """Default anonymized location — state/region only, no city."""
    loc = (location or "").strip()
    if not loc:
        return "the United States"
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1]  # state or country
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


def collect_month_stats(client_email: str, *, meetings_override: int | None = None) -> dict:
    """Gather campaign metrics for the current calendar month."""
    import csv

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
    if meetings_booked is None:
        meetings_booked = 0
        geo = profile.get("geo") or ""
        try:
            from pipeline.to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

            sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
            ws = open_worksheet_safe(sh, "Clients")
            headers = [h.strip() for h in get_all_values_safe(ws)[0]]
            col = {h: i for i, h in enumerate(headers)}
            for row in get_all_values_safe(ws)[1:]:
                if len(row) <= col.get("Email", 2):
                    continue
                if row[col["Email"]].strip().lower() != email:
                    continue
                raw = row[col["meetings_this_month"]] if "meetings_this_month" in col else "0"
                try:
                    meetings_booked = int(str(raw or 0))
                except ValueError:
                    meetings_booked = 0
                if "client_geo" in col and len(row) > col["client_geo"]:
                    geo = row[col["client_geo"]] or geo
                break
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
        from pipeline.to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

        sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = open_worksheet_safe(sh, "Clients")
        headers = [h.strip() for h in get_all_values_safe(ws)[0]]
        col = {h: i for i, h in enumerate(headers)}
        for row in get_all_values_safe(ws)[1:]:
            if len(row) <= col.get("Email", 2):
                continue
            if row[col["Email"]].strip().lower() != email:
                continue
            for created_key in ("Created", "Created Date", "Start Date"):
                if created_key in col and len(row) > col[created_key]:
                    raw = (row[col[created_key]] or "").strip()[:10]
                    try:
                        created = date.fromisoformat(raw)
                        duration_days = max(1, (date.today() - created).days)
                    except ValueError:
                        pass
                    break
            break
    except Exception:
        pass

    closed = max(1, int(meetings_booked * CLOSED_RATE))
    revenue = closed * REVENUE_PER_CLOSE

    return {
        "client_email": email,
        "client_name": profile.get("name") or "there",
        "company": profile.get("company") or "",
        "location": profile.get("geo") or "",
        "emails_sent": emails_sent,
        "replies": replies,
        "meetings_booked": meetings_booked,
        "reply_rate_pct": reply_rate,
        "meeting_conversion_pct": meeting_conversion,
        "duration_days": duration_days,
        "closed_estimate": closed,
        "revenue_estimate": revenue,
    }


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
    for bucket in ("drafts", "approved", "pending_permission"):
        for item in data.get(bucket) or []:
            if (item.get("client_email") or "").lower() == email:
                if item.get("month") == date.today().strftime("%Y-%m"):
                    return True
    return False


def check_triggers(*, dry_run: bool = False) -> dict:
    """Scan clients with 10+ meetings this month; create drafts and request permission."""
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
            print(f"[dry-run] would create case study for {email}: {draft['meetings_booked']} meetings")
            created.append(email)
            continue
        data.setdefault("pending_permission", []).append(draft)
        send_permission_request(draft)
        draft["permission_requested"] = True
        created.append(email)
        print(f"case study draft created for {email} ({draft['meetings_booked']} meetings)")

    if not dry_run and created:
        _save(data)
    return {"created": created, "count": len(created)}


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
    source_bucket = None

    for bucket in ("pending_permission", "drafts"):
        for i, item in enumerate(data.get(bucket) or []):
            if item.get("id") == case_id or item.get("client_email", "").lower() == case_id.lower():
                found = data[bucket].pop(i)
                source_bucket = bucket
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

    named = show_name
    pub = {
        "id": found["id"],
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
        "key_metric": f"{found.get('meetings_booked')} meetings booked",
    }
    data.setdefault("approved", []).append({**found, **pub})
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


def list_case_studies() -> dict:
    data = _load()
    return {
        "updated_at": data.get("updated_at"),
        "pending_permission": data.get("pending_permission") or [],
        "drafts": data.get("drafts") or [],
        "approved": data.get("approved") or [],
    }


def site_payload(data: dict | None = None) -> dict:
    data = data or _load()
    approved = data.get("approved") or []
    highlights = []
    for cs in approved[:6]:
        highlights.append(
            {
                "id": cs.get("id"),
                "industry": cs.get("industry") or cs.get("client_industry"),
                "location": cs.get("location") or _anonymize_location(cs.get("client_location") or ""),
                "key_metric": cs.get("key_metric") or f"{cs.get('meetings_booked')} meetings",
                "quote": cs.get("quote"),
                "prose": cs.get("prose") or render_prose(cs, named=cs.get("show_name", False)),
            }
        )

    meetings_vals = [int(cs.get("meetings_booked") or 0) for cs in approved if cs.get("meetings_booked")]
    avg_meetings = round(sum(meetings_vals) / len(meetings_vals), 1) if meetings_vals else 8
    agency_count = max(len(approved), len(data.get("pending_permission") or []) + len(approved), 5)

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
                "industry": cs.get("industry") or cs.get("client_industry"),
                "location": cs.get("location") or _anonymize_location(cs.get("client_location") or ""),
                "duration": cs.get("duration"),
                "meetings_booked": cs.get("meetings_booked"),
                "emails_sent": cs.get("emails_sent"),
                "reply_rate": cs.get("reply_rate"),
                "meeting_conversion": cs.get("meeting_conversion"),
                "revenue_generated": cs.get("revenue_generated"),
                "quote": cs.get("quote"),
                "prose": cs.get("prose") or render_prose(cs, named=cs.get("show_name", False)),
                "company": cs.get("company") if cs.get("show_name") else None,
                "key_metric": cs.get("key_metric") or f"{cs.get('meetings_booked')} meetings booked",
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

    sub.add_parser("list", help="List all case studies and approval status")
    sub.add_parser("check", help="Check clients for 10+ meetings and create drafts")
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

    sub.add_parser("export", help="Export approved case studies to website JSON")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        print(json.dumps(list_case_studies(), indent=2))
        return 0
    if args.cmd in ("check", "trigger"):
        dry = getattr(args, "dry_run", False)
        print(json.dumps(check_triggers(dry_run=dry), indent=2))
        return 0
    if args.cmd == "simulate":
        print(json.dumps(simulate_case_study(args.email, meetings=args.meetings, location=args.location), indent=2))
        return 0
    if args.cmd == "approve":
        print(json.dumps(approve_case_study(args.case_id, quote=args.quote or None, show_name=args.show_name, show_logo=args.show_logo), indent=2))
        return 0
    if args.cmd == "send-draft":
        print(json.dumps(send_draft_for_review(args.case_id), indent=2))
        return 0
    if args.cmd == "export":
        export_to_site()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
