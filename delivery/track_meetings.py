"""Track meetings from client Calendly API and/or Google Calendar OAuth."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "data" / "reports"


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _event_status(ev: dict, guest: dict, end_dt: datetime | None) -> str:
    """Map to: scheduled | completed | no-show | cancelled."""
    if (ev.get("status") or "").lower() == "cancelled":
        return "cancelled"
    if guest.get("responseStatus") == "declined":
        return "cancelled"
    desc = ((ev.get("description") or "") + " " + (ev.get("summary") or "")).lower()
    if "no-show" in desc or "no show" in desc:
        return "no-show"
    if guest.get("noShow") or guest.get("no_show"):
        return "no-show"

    now = datetime.now(timezone.utc)
    if end_dt:
        end_cmp = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
        if end_cmp > now:
            return "scheduled"
        return "completed"
    return "scheduled"


def _normalize_meeting(raw: dict) -> dict:
    """Canonical meeting shape for qualify / billing / dashboard."""
    status = (raw.get("status") or "scheduled").lower()
    if status in ("upcoming", "active", "confirmed"):
        status = "scheduled"
    if status in ("canceled",):
        status = "cancelled"

    event_date = raw.get("event_date") or raw.get("date") or ""
    start = raw.get("start_time") or ""
    if not event_date and start:
        event_date = start[:10]

    return {
        "event_date": event_date,
        "date": event_date,  # alias used by qualify_meetings
        "attendee_name": raw.get("attendee_name") or "",
        "attendee_email": raw.get("attendee_email") or "",
        "attendee_company": raw.get("attendee_company")
        or raw.get("practice_name")
        or raw.get("summary")
        or "",
        "practice_name": raw.get("practice_name") or raw.get("summary") or "",
        "summary": raw.get("summary") or "",
        "duration_minutes": raw.get("duration_minutes"),
        "status": status,
        "start_time": start,
        "end_time": raw.get("end_time") or "",
        "tagged": bool(raw.get("tagged")),
        "html_link": raw.get("html_link") or "",
        "no_show": status == "no-show" or bool(raw.get("no_show")),
        "source": raw.get("source") or "",
    }


def _calendly_token_for_client(client_email: str) -> str:
    """Per-client token from Clients sheet, else env CALENDLY_API_TOKEN."""
    email = (client_email or "").strip().lower()
    try:
        from pipeline.to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

        sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = open_worksheet_safe(sh, "Clients")
        values = get_all_values_safe(ws)
        if not values:
            return os.environ.get("CALENDLY_API_TOKEN", "").strip()
        headers = [h.strip() for h in values[0]]
        col = {h.lower(): i for i, h in enumerate(headers)}
        email_i = col.get("email", 2)
        token_i = None
        for key in (
            "calendly api token",
            "calendly_api_token",
            "calendly token",
        ):
            if key in col:
                token_i = col[key]
                break
        if token_i is not None:
            for row in values[1:]:
                if len(row) > email_i and row[email_i].strip().lower() == email:
                    tok = (row[token_i] if token_i < len(row) else "").strip()
                    if tok:
                        return tok
    except Exception as e:
        print(f"Calendly token sheet lookup skipped: {e}")
    return os.environ.get("CALENDLY_API_TOKEN", "").strip()


def fetch_calendar_events(client_email: str, days: int = 30) -> list[dict]:
    """List events from the client's primary Google Calendar (OAuth)."""
    from googleapiclient.discovery import build

    from .connect_workspace import refresh_tokens

    creds = refresh_tokens(client_email)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days)).isoformat()
    time_max = (now + timedelta(days=2)).isoformat()

    events: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            attendees = ev.get("attendees") or []
            guest = next(
                (
                    a
                    for a in attendees
                    if not a.get("self")
                    and a.get("email", "").lower() != client_email.lower()
                ),
                {},
            )
            start_raw = (ev.get("start") or {}).get("dateTime") or (
                ev.get("start") or {}
            ).get("date", "")
            end_raw = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get(
                "date", ""
            )
            start_dt = _parse_dt(start_raw)
            end_dt = _parse_dt(end_raw)
            duration = None
            if start_dt and end_dt:
                duration = max(0.0, (end_dt - start_dt).total_seconds() / 60.0)

            desc = (ev.get("description") or "") + " " + (ev.get("summary") or "")
            desc_l = desc.lower()
            tagged = any(
                tag in desc_l
                for tag in (
                    "utm_source=dentalmeet",
                    "utm_source%3ddentalmeet",
                    "dentalmeet",
                    "calendly",
                )
            )
            status = _event_status(ev, guest, end_dt)
            events.append(
                _normalize_meeting(
                    {
                        "event_date": (start_raw or "")[:10],
                        "summary": ev.get("summary", ""),
                        "attendee_name": guest.get("displayName", ""),
                        "attendee_email": guest.get("email", ""),
                        "attendee_company": ev.get("summary", ""),
                        "practice_name": ev.get("summary", ""),
                        "tagged": tagged,
                        "html_link": ev.get("htmlLink", ""),
                        "status": status,
                        "start_time": start_raw,
                        "end_time": end_raw,
                        "duration_minutes": duration,
                        "source": "google_calendar",
                    }
                )
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def fetch_calendly_events(client_email: str, days: int = 30) -> list[dict]:
    """Calendly API for client token, else Google Calendar fallback."""
    return get_calendly_events(client_email, days=days)


def get_calendly_events(client_email: str, days: int = 30) -> list[dict]:
    """
    Fetch meetings for the last N days.

    Prefers client Calendly API token (Clients sheet / env), else Google Calendar.
    Filters to utm_source=dentalmeet / dentalmeet-tagged when any tagged events exist.
    """
    import requests

    token = _calendly_token_for_client(client_email)
    if not token:
        print("No Calendly API token — falling back to Google Calendar OAuth")
        events = fetch_calendar_events(client_email, days=days)
        tagged = [e for e in events if e.get("tagged")]
        return tagged or events

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    me = requests.get("https://api.calendly.com/users/me", headers=headers, timeout=30)
    me.raise_for_status()
    user_uri = me.json()["resource"]["uri"]
    now = datetime.now(timezone.utc)
    params = {
        "user": user_uri,
        "min_start_time": (now - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
        "max_start_time": (now + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    }
    # Fetch active + canceled
    out: list[dict] = []
    for status_filter in (None, "canceled"):
        p = dict(params)
        if status_filter:
            p["status"] = status_filter
        r = requests.get(
            "https://api.calendly.com/scheduled_events",
            headers=headers,
            params=p,
            timeout=30,
        )
        if r.status_code != 200:
            if status_filter is None:
                r.raise_for_status()
            continue
        for ev in r.json().get("collection", []):
            start_raw = ev.get("start_time") or ""
            end_raw = ev.get("end_time") or ""
            start_dt = _parse_dt(start_raw)
            end_dt = _parse_dt(end_raw)
            duration = None
            if start_dt and end_dt:
                duration = max(0.0, (end_dt - start_dt).total_seconds() / 60.0)

            cal_status = (ev.get("status") or "").lower()
            if cal_status in ("canceled", "cancelled"):
                status = "cancelled"
            elif end_dt and (
                end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
            ) > now:
                status = "scheduled"
            else:
                status = "completed"

            attendee_email = ""
            attendee_name = ""
            attendee_company = ""
            no_show = False
            tracking = {}
            invitees_url = ev.get("uri")
            if invitees_url:
                try:
                    ir = requests.get(
                        f"{invitees_url}/invitees",
                        headers=headers,
                        timeout=30,
                    )
                    if ir.status_code == 200:
                        inv = (ir.json().get("collection") or [None])[0] or {}
                        attendee_email = inv.get("email") or ""
                        attendee_name = inv.get("name") or ""
                        no_show = bool(inv.get("no_show"))
                        if no_show:
                            status = "no-show"
                        tracking = inv.get("tracking") or {}
                        qs = inv.get("questions_and_answers") or []
                        for qa in qs:
                            q = (qa.get("question") or "").lower()
                            if "company" in q or "practice" in q:
                                attendee_company = qa.get("answer") or ""
                except Exception as e:
                    print(f"Calendly invitees fetch skipped: {e}")

            utm = (
                (tracking.get("utm_source") or "")
                + " "
                + (ev.get("name") or "")
            ).lower()
            utm_source = (tracking.get("utm_source") or "").lower()
            tagged = utm_source == "dentalmeet" or "dentalmeet" in utm

            out.append(
                _normalize_meeting(
                    {
                        "event_date": start_raw[:10],
                        "summary": ev.get("name", ""),
                        "attendee_name": attendee_name,
                        "attendee_email": attendee_email,
                        "attendee_company": attendee_company or ev.get("name", ""),
                        "practice_name": attendee_company or ev.get("name", ""),
                        "tagged": tagged,
                        "html_link": "",
                        "status": status,
                        "start_time": start_raw,
                        "end_time": end_raw,
                        "duration_minutes": duration,
                        "no_show": no_show,
                        "source": "calendly",
                    }
                )
            )

    # Dedup by start+email
    seen: set[str] = set()
    unique: list[dict] = []
    for e in out:
        key = f"{e.get('start_time')}|{e.get('attendee_email')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    # Prefer utm_source=dentalmeet when any exist; else all Calendly events
    dental = [e for e in unique if e.get("tagged")]
    return dental or unique


def generate_meetings_dashboard(
    client_email: str,
    days: int = 30,
    *,
    with_qualification: bool = True,
) -> Path:
    """HTML table: Date, Attendee, Company, Duration, Status, Qualified."""
    meetings = get_calendly_events(client_email, days=days)
    qual_map: dict[str, tuple[bool, str]] = {}
    if with_qualification:
        try:
            from .qualify_meetings import qualify_all_meetings

            period = "month" if days >= 28 else "week"
            result = qualify_all_meetings(client_email, period=period)
            for item in result.get("breakdown") or []:
                m = item.get("meeting") or {}
                key = f"{m.get('event_date') or m.get('date')}|{m.get('attendee_email')}"
                qual_map[key] = (bool(item.get("qualified")), item.get("reason") or "")
        except Exception as e:
            print(f"qualification for dashboard skipped: {e}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = client_email.strip().lower().replace("@", "_at_")
    path = REPORTS_DIR / f"{safe}_meetings.html"
    rows = []
    for m in meetings:
        key = f"{m.get('event_date')}|{m.get('attendee_email')}"
        q, reason = qual_map.get(key, (None, ""))
        q_txt = "—" if q is None else ("Yes" if q else f"No ({reason})")
        rows.append(
            "<tr>"
            f"<td>{m.get('event_date', '')}</td>"
            f"<td>{m.get('attendee_name', '')}<br><span class='muted'>{m.get('attendee_email', '')}</span></td>"
            f"<td>{m.get('attendee_company', '')}</td>"
            f"<td>{m.get('duration_minutes') if m.get('duration_minutes') is not None else '—'}</td>"
            f"<td>{m.get('status', '')}</td>"
            f"<td>{q_txt}</td>"
            "</tr>"
        )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Meetings — {client_email}</title>
<style>
body{{font-family:Georgia,serif;max-width:960px;margin:2rem auto;background:#f7f4ef;color:#1a1a1a;padding:1rem}}
table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{text-align:left;padding:0.55rem;border-bottom:1px solid #ddd;font-size:0.9rem;vertical-align:top}}
.muted{{color:#666;font-size:0.8rem}}
</style></head><body>
<h1>Meetings</h1>
<p class="muted">{client_email} · last {days} days · {len(meetings)} events</p>
<table>
<thead><tr><th>Date</th><th>Attendee</th><th>Company</th><th>Duration</th><th>Status</th><th>Qualified</th></tr></thead>
<tbody>
{"".join(rows) if rows else "<tr><td colspan='6'>No meetings found</td></tr>"}
</tbody></table>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    print(f"dashboard: {path}")
    return path


def count_billable_meetings(client_email: str, period: str = "month") -> dict:
    """Count ONLY qualified meetings and compute billable amount (pilot/repeat)."""
    from .billing import calculate_invoice

    inv = calculate_invoice(client_email, period=period, write_files=False)
    return {
        "client_email": client_email,
        "period": period,
        "total_meetings": inv["total_meetings"],
        "qualified_meetings": inv["qualified_meetings"],
        "disqualified_meetings": inv["disqualified_meetings"],
        "billable_amount": inv["amount"],
        "pricing_model": inv["model"],
        "invoice_line": inv["invoice_line"],
        "meetings": [b["meeting"] for b in inv["breakdown"] if b["qualified"]],
        "breakdown": inv["breakdown"],
        "html_path": inv.get("html_path", ""),
        "pdf_path": inv.get("pdf_path", ""),
    }


def update_clients_meeting_count(client_email: str, total: int) -> None:
    from pipeline.to_sheets import (
        get_all_values_safe,
        open_sheet_safe,
        open_worksheet_safe,
        row_values_safe,
        update_cell_safe,
    )

    sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
    ws = open_worksheet_safe(sh, "Clients")
    values = get_all_values_safe(ws)
    headers = values[0]
    if "Meetings" not in headers:
        update_cell_safe(ws, 1, len(headers) + 1, "Meetings")
        headers = row_values_safe(ws, 1)
    mcol = headers.index("Meetings") + 1
    ecol = headers.index("Email") + 1 if "Email" in headers else 3
    for i, row in enumerate(values[1:], start=2):
        if len(row) >= ecol and row[ecol - 1].strip().lower() == client_email.lower():
            update_cell_safe(ws, i, mcol, str(total))
            print(f"Clients row {i}: Meetings={total}")
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track client meetings")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch Calendly/Calendar events")
    p_fetch.add_argument("client_email")
    p_fetch.add_argument("--days", type=int, default=30)

    p_list = sub.add_parser("list", help="List meetings (table)")
    p_list.add_argument("client_email")
    p_list.add_argument("--days", type=int, default=30)

    p_dash = sub.add_parser("dashboard", help="Write HTML meetings dashboard")
    p_dash.add_argument("client_email")
    p_dash.add_argument("--days", type=int, default=30)

    p_count = sub.add_parser("count", help="Count billable (qualified) meetings")
    p_count.add_argument("client_email")
    p_count.add_argument("--period", default="week", choices=["week", "month"])
    p_count.add_argument("--update-sheet", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        events = get_calendly_events(args.client_email, days=args.days)
        print(f"fetched {len(events)} meetings")
        for e in events:
            print(
                f"  {e.get('event_date')} | {e.get('status')} | "
                f"{e.get('attendee_email')} | {e.get('duration_minutes')}m | "
                f"{e.get('attendee_company')}"
            )
        return 0

    if args.cmd == "list":
        events = get_calendly_events(args.client_email, days=args.days)
        print(
            f"{'Date':<12} {'Status':<12} {'Mins':>5}  {'Attendee':<32} Company"
        )
        print("-" * 90)
        for e in events:
            mins = e.get("duration_minutes")
            mins_s = f"{int(mins)}" if mins is not None else "—"
            print(
                f"{(e.get('event_date') or ''):<12} "
                f"{(e.get('status') or ''):<12} "
                f"{mins_s:>5}  "
                f"{(e.get('attendee_email') or '')[:32]:<32} "
                f"{(e.get('attendee_company') or '')[:40]}"
            )
        print(f"\nTotal: {len(events)}")
        return 0

    if args.cmd == "dashboard":
        generate_meetings_dashboard(args.client_email, days=args.days)
        return 0

    if args.cmd == "count":
        result = count_billable_meetings(args.client_email, period=args.period)
        if args.update_sheet:
            update_clients_meeting_count(
                args.client_email, result["qualified_meetings"]
            )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
