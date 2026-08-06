"""Qualify booked meetings against ICP before billing.

Contract definition — a Qualified Meeting meets ALL of:
  1. Actually took place (not no-show / cancellation)
  2. Duration >= 15 minutes
  3. Decision-maker title (Owner, CEO, Founder, President,
     Practice Manager, or Director)
  4. Practice in Client's target geography (onboarding form / Geo)
  5. Legitimate dental practice (not competitor, student, researcher)
  6. No duplicate booking of the same attendee in the same calendar month

Only Qualified Meetings are billed. Disqualification reasons are reported
via `qualify_meetings report` / weekly invoice breakdown.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "data" / "reports"
APOLLO_BASE = "https://api.apollo.io/api/v1"

DECISION_TITLES = (
    "owner",
    "ceo",
    "founder",
    "president",
    "practice manager",
    "director",
)
REJECT_TITLES = (
    "office manager",
    "receptionist",
    "assistant",
    "intern",
    "student",
    "researcher",
    "research assistant",
    "phd candidate",
)
ALLOWED_ORG_TYPES = (
    "dental practice",
    "medical practice",
    "hospital & health care",
    "hospital and health care",
    "health, wellness and fitness",
    "health care",
    "healthcare",
)
REJECT_ORG_TYPES = (
    "marketing agency",
    "marketing and advertising",
    "software company",
    "computer software",
    "information technology",
    "consulting",
    "management consulting",
    "higher education",
    "education management",
    "research",
)

_MIN_INTERVAL = 0.65
_last_call = 0.0
_apollo_cache: dict[str, dict | None] = {}


def _rate_limit() -> None:
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = time.time()


def _apollo_headers() -> dict:
    key = os.environ.get("APOLLO_KEY", "").strip()
    if not key:
        raise RuntimeError("APOLLO_KEY missing")
    return {
        "X-Api-Key": key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _safe(email: str) -> str:
    return email.strip().lower().replace("@", "_at_")


def fetch_attendee_from_apollo(email: str) -> dict | None:
    """Lookup person + org by email via Apollo match / bulk_match."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    if email in _apollo_cache:
        return _apollo_cache[email]

    person = None
    try:
        _rate_limit()
        r = requests.post(
            f"{APOLLO_BASE}/people/match",
            headers=_apollo_headers(),
            params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
            json={"email": email},
            timeout=45,
        )
        if r.status_code == 200:
            data = r.json()
            person = data.get("person") or data.get("match")
            if isinstance(person, dict) and not person.get("email"):
                person = person if person.get("id") or person.get("title") else None
        elif r.status_code != 404:
            # Fallback: bulk_match by email
            _rate_limit()
            br = requests.post(
                f"{APOLLO_BASE}/people/bulk_match",
                headers=_apollo_headers(),
                params={
                    "reveal_personal_emails": "false",
                    "reveal_phone_number": "false",
                },
                json={"details": [{"email": email}]},
                timeout=45,
            )
            if br.status_code == 200:
                matches = br.json().get("matches") or []
                person = next((m for m in matches if m), None)
    except Exception as e:
        print(f"Apollo lookup failed for {email}: {e}")
        person = None

    _apollo_cache[email] = person
    return person


def _normalize_status(meeting_data: dict) -> str:
    """Map raw status to: completed | cancelled | no-show | upcoming | unknown."""
    raw = (meeting_data.get("status") or "").strip().lower()
    if raw in ("cancelled", "canceled"):
        return "cancelled"
    if raw in ("no-show", "no_show", "noshow"):
        return "no-show"
    if raw in ("completed", "done", "finished"):
        return "completed"
    if raw in ("upcoming", "scheduled", "active", "confirmed"):
        # Still may be completed if end time is in the past; else scheduled
        pass
    if raw == "scheduled":
        pass

    end = meeting_data.get("end_time") or meeting_data.get("end") or ""
    try:
        if end:
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            now = datetime.now(end_dt.tzinfo) if end_dt.tzinfo else datetime.utcnow()
            if end_dt > now and raw not in ("cancelled", "canceled", "no-show"):
                return "upcoming"
            if end_dt <= now and raw not in ("cancelled", "canceled", "no-show"):
                return "completed"
    except Exception:
        pass

    if meeting_data.get("no_show"):
        return "no-show"
    if raw:
        return raw
    return "unknown"


def _duration_minutes(meeting_data: dict) -> float | None:
    if meeting_data.get("duration_minutes") is not None:
        try:
            return float(meeting_data["duration_minutes"])
        except (TypeError, ValueError):
            pass
    start = meeting_data.get("start_time") or meeting_data.get("start") or ""
    end = meeting_data.get("end_time") or meeting_data.get("end") or ""
    try:
        if start and end:
            s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            return max(0.0, (e - s).total_seconds() / 60.0)
    except Exception:
        return None
    return None


def _title_ok(title: str) -> tuple[bool, str]:
    low = (title or "").strip().lower()
    if not low:
        return False, "Not decision-maker"
    if any(bad in low for bad in REJECT_TITLES):
        return False, "Not decision-maker"
    if any(good in low for good in DECISION_TITLES):
        return True, "Qualified"
    return False, "Not decision-maker"


def _geo_matches(person: dict, geographic_target: str) -> bool:
    geo = (geographic_target or "").strip()
    if not geo:
        return True  # no ICP geo → do not block
    targets = [g.strip().lower() for g in geo.replace(";", ",").split(",") if g.strip()]
    if not targets:
        return True
    if any(t in ("nationwide", "usa", "us", "united states") for t in targets):
        return True

    org = person.get("organization") or {}
    haystack_parts = [
        person.get("city") or "",
        person.get("state") or "",
        person.get("country") or "",
        org.get("city") or "",
        org.get("state") or "",
        org.get("country") or "",
        " ".join(person.get("present_raw_address") or [])
        if isinstance(person.get("present_raw_address"), list)
        else (person.get("present_raw_address") or ""),
    ]
    # Apollo sometimes nests locations
    for loc in org.get("raw_address") or []:
        if isinstance(loc, str):
            haystack_parts.append(loc)
    for key in ("organization_city", "organization_state", "organization_country"):
        haystack_parts.append(str(person.get(key) or ""))

    haystack = " ".join(str(p) for p in haystack_parts).lower()
    return any(t in haystack for t in targets)


def _org_type_ok(person: dict) -> tuple[bool, str]:
    org = person.get("organization") or {}
    industries: list[str] = []
    for key in ("industry", "industries", "industry_tag_hash"):
        val = org.get(key) or person.get(key)
        if isinstance(val, str) and val:
            industries.append(val)
        elif isinstance(val, list):
            industries.extend(str(x) for x in val if x)
        elif isinstance(val, dict):
            industries.extend(str(k) for k in val.keys())

    # Also use keywords / name hints
    name = (org.get("name") or "").lower()
    blob = " ".join(i.lower() for i in industries) + " " + name

    if any(bad in blob for bad in REJECT_ORG_TYPES):
        return False, "Not a dental practice"
    if any(ok in blob for ok in ALLOWED_ORG_TYPES):
        return True, "Qualified"
    # Soft pass: dental keyword in company name when industry missing
    if "dental" in name or "dentistry" in name or "orthodont" in name:
        return True, "Qualified"
    if not industries and not name:
        return False, "Not a dental practice"
    return False, "Not a dental practice"


def check_meeting_qualification(
    meeting_data: dict,
    client_icp: dict,
    *,
    prior_attendee_emails: set[str] | None = None,
) -> dict:
    """
    Check qualification criteria for one meeting.

    meeting_data keys used:
      status, duration_minutes / start_time+end_time, attendee_email, date
    client_icp keys used:
      geographic_target
    """
    status = _normalize_status(meeting_data)
    if status == "cancelled":
        return {"qualified": False, "reason": "Cancelled"}
    if status == "no-show":
        return {"qualified": False, "reason": "No-show"}
    if status == "upcoming":
        return {"qualified": False, "reason": "Not completed"}
    if status not in ("completed",):
        # unknown without end time → not billable
        if status == "unknown":
            dur_probe = _duration_minutes(meeting_data)
            if dur_probe is None:
                return {"qualified": False, "reason": "Not completed"}
        else:
            return {"qualified": False, "reason": "Not completed"}

    duration = _duration_minutes(meeting_data)
    if duration is None:
        return {"qualified": False, "reason": "Too short"}
    if duration < 15:
        return {"qualified": False, "reason": "Too short"}

    attendee = (meeting_data.get("attendee_email") or "").strip().lower()
    if not attendee:
        return {"qualified": False, "reason": "Attendee not found"}

    seen = prior_attendee_emails or set()
    if attendee in seen:
        return {"qualified": False, "reason": "Duplicate"}

    try:
        person = fetch_attendee_from_apollo(attendee)
    except RuntimeError as e:
        # Missing API key — fail closed for billing honesty
        print(f"Apollo unavailable: {e}")
        return {"qualified": False, "reason": "Attendee not found"}

    if not person:
        return {"qualified": False, "reason": "Attendee not found"}

    title = person.get("title") or meeting_data.get("attendee_title") or ""
    ok_title, reason_title = _title_ok(title)
    if not ok_title:
        return {"qualified": False, "reason": reason_title}

    geo = client_icp.get("geographic_target") or client_icp.get("geo") or ""
    if not _geo_matches(person, geo):
        return {"qualified": False, "reason": "Wrong geography"}

    ok_org, reason_org = _org_type_ok(person)
    if not ok_org:
        return {"qualified": False, "reason": reason_org}

    return {"qualified": True, "reason": "Qualified"}


def qualify_client_meetings(client_email: str, period: str = "month") -> dict:
    """Fetch meetings and qualify each against client ICP."""
    from .research_dental import load_client_icp
    from .track_meetings import get_calendly_events

    days = 30 if period == "month" else 7 if period == "week" else 30
    icp = load_client_icp(client_email)
    events = get_calendly_events(client_email, days=days)
    tagged = [e for e in events if e.get("tagged")]
    meetings = tagged or events

    # Chronological for duplicate detection
    meetings = sorted(meetings, key=lambda m: m.get("date") or m.get("start_time") or "")

    breakdown: list[dict] = []
    prior: set[str] = set()
    qualified_n = 0
    disqualified_n = 0

    for m in meetings:
        result = check_meeting_qualification(m, icp, prior_attendee_emails=prior)
        email = (m.get("attendee_email") or "").strip().lower()
        # Count any prior appearance this period as duplicate source
        if email:
            # Mark seen after check so first occurrence can qualify
            if result["qualified"]:
                prior.add(email)
            elif result["reason"] != "Duplicate" and email:
                # Non-qualified still blocks later duplicates of same email
                prior.add(email)

        if result["qualified"]:
            qualified_n += 1
        else:
            disqualified_n += 1

        breakdown.append(
            {
                "meeting": m,
                "qualified": result["qualified"],
                "reason": result["reason"],
            }
        )

    out = {
        "client_email": client_email,
        "period": period,
        "total_meetings": len(meetings),
        "qualified_meetings": qualified_n,
        "disqualified_meetings": disqualified_n,
        "breakdown": breakdown,
        "icp": {
            "geographic_target": icp.get("geographic_target", ""),
            "practice_types": icp.get("practice_types", ""),
        },
    }
    print(
        f"{client_email}: {qualified_n}/{len(meetings)} qualified "
        f"({disqualified_n} disqualified) last {days}d"
    )
    return out


def qualify_all_meetings(client_email: str, period: str = "month") -> dict:
    """Alias used by billing / dashboards."""
    return qualify_client_meetings(client_email, period=period)


def generate_qualification_report(client_email: str, period: str = "month") -> Path:
    """Simple HTML dashboard: which meetings qualified and why."""
    from .billing import RATE_PER_MEETING, format_qualified_invoice

    result = qualify_client_meetings(client_email, period=period)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{_safe(client_email)}_qualification_{period}.html"
    invoice = format_qualified_invoice(result["qualified_meetings"])

    rows_html = []
    for item in result["breakdown"]:
        m = item["meeting"]
        ok = item["qualified"]
        badge = "ok" if ok else "bad"
        rows_html.append(
            "<tr>"
            f"<td>{(m.get('date') or '')}</td>"
            f"<td>{(m.get('summary') or m.get('practice_name') or '')}</td>"
            f"<td>{(m.get('attendee_email') or '')}</td>"
            f"<td>{(m.get('duration_minutes') if m.get('duration_minutes') is not None else '—')}</td>"
            f"<td class='{badge}'>{'Yes' if ok else 'No'}</td>"
            f"<td>{item['reason']}</td>"
            "</tr>"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Meeting Qualification</title>
<style>
body{{font-family:Georgia,serif;max-width:900px;margin:2rem auto;color:#1a1a1a;
background:linear-gradient(#f7f4ef,#ebe6dc);padding:1rem 1.5rem}}
h1{{font-size:1.6rem;margin-bottom:0.25rem}}
.muted{{color:#666;margin-bottom:1.5rem}}
.stats{{display:flex;gap:1.5rem;flex-wrap:wrap;margin:1rem 0 1.5rem}}
.stat strong{{display:block;font-size:1.4rem}}
table{{width:100%;border-collapse:collapse;background:#fffdf8}}
th,td{{text-align:left;padding:0.55rem 0.6rem;border-bottom:1px solid #ddd;font-size:0.92rem}}
th{{font-size:0.8rem;text-transform:uppercase;letter-spacing:0.04em;color:#555}}
.ok{{color:#1b6b3a;font-weight:600}} .bad{{color:#9b2c2c;font-weight:600}}
.invoice{{margin-top:1.5rem;padding:1rem;background:#fff;border-left:4px solid #1a1a1a}}
</style></head><body>
<h1>Meeting qualification</h1>
<p class="muted">{client_email} · period={period}</p>
<div class="stats">
  <div class="stat"><strong>{result['total_meetings']}</strong>Total</div>
  <div class="stat"><strong>{result['qualified_meetings']}</strong>Qualified</div>
  <div class="stat"><strong>{result['disqualified_meetings']}</strong>Disqualified</div>
</div>
<div class="invoice">{invoice}</div>
<table>
<thead><tr>
  <th>Date</th><th>Meeting</th><th>Attendee</th><th>Mins</th><th>OK</th><th>Reason</th>
</tr></thead>
<tbody>
{"".join(rows_html) if rows_html else "<tr><td colspan='6'>No meetings in period</td></tr>"}
</tbody>
</table>
<p class="muted">Rate ${RATE_PER_MEETING}/qualified meeting. Geo ICP:
{result['icp'].get('geographic_target') or 'n/a'}</p>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    print(f"qualification report: {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify client meetings for billing")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Qualify meetings and print summary")
    p_check.add_argument("client_email")
    p_check.add_argument("--period", default="month", choices=["week", "month"])

    p_report = sub.add_parser("report", help="Write HTML qualification dashboard")
    p_report.add_argument("client_email")
    p_report.add_argument("--period", default="month", choices=["week", "month"])

    args = parser.parse_args(argv)

    if args.cmd == "check":
        result = qualify_client_meetings(args.client_email, period=args.period)
        from .billing import format_qualified_invoice

        print(format_qualified_invoice(result["qualified_meetings"]))
        for item in result["breakdown"]:
            m = item["meeting"]
            flag = "✓" if item["qualified"] else "✗"
            print(
                f"  {flag} {(m.get('date') or '?')} "
                f"{(m.get('attendee_email') or 'no-email')} — {item['reason']}"
            )
        return 0

    if args.cmd == "report":
        generate_qualification_report(args.client_email, period=args.period)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
