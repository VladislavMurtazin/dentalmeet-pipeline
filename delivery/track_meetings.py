"""Track meetings from Google Calendar (client OAuth) for billing."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


def fetch_calendar_events(client_email: str, days: int = 7) -> list[dict]:
    """List events from the client's primary Google Calendar."""
    from googleapiclient.discovery import build

    from .connect_workspace import refresh_tokens

    creds = refresh_tokens(client_email)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days)).isoformat()
    time_max = now.isoformat()

    events = []
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
            start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get(
                "date", ""
            )
            desc = (ev.get("description") or "") + " " + (ev.get("summary") or "")
            # Prefer events that look like outreach bookings
            tagged = any(
                tag in desc.lower()
                for tag in ("dentalmeet", "utm_source", "calendly", "new patient")
            )
            events.append(
                {
                    "date": start[:10] if start else "",
                    "summary": ev.get("summary", ""),
                    "attendee_name": guest.get("displayName", ""),
                    "attendee_email": guest.get("email", ""),
                    "practice_name": ev.get("summary", ""),
                    "tagged": tagged,
                    "html_link": ev.get("htmlLink", ""),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def fetch_calendly_events(client_email: str, days: int = 7) -> list[dict]:
    """Optional Calendly API (needs CALENDLY_API_TOKEN in env or per-client)."""
    import requests

    token = os.environ.get("CALENDLY_API_TOKEN", "").strip()
    if not token:
        print("CALENDLY_API_TOKEN not set — falling back to Google Calendar")
        return fetch_calendar_events(client_email, days=days)

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    me = requests.get("https://api.calendly.com/users/me", headers=headers, timeout=30)
    me.raise_for_status()
    user_uri = me.json()["resource"]["uri"]
    now = datetime.now(timezone.utc)
    params = {
        "user": user_uri,
        "min_start_time": (now - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
        "max_start_time": now.isoformat().replace("+00:00", "Z"),
        "status": "active",
    }
    r = requests.get(
        "https://api.calendly.com/scheduled_events",
        headers=headers,
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    out = []
    for ev in r.json().get("collection", []):
        out.append(
            {
                "date": (ev.get("start_time") or "")[:10],
                "summary": ev.get("name", ""),
                "attendee_name": "",
                "attendee_email": "",
                "practice_name": ev.get("name", ""),
                "tagged": True,
                "html_link": "",
            }
        )
    return out


def count_billable_meetings(client_email: str, period: str = "month") -> dict:
    """Count meetings and compute billable amount."""
    days = 30 if period == "month" else 7 if period == "week" else 30
    events = fetch_calendly_events(client_email, days=days)
    # Prefer tagged; if none tagged, count all 1:1 style events
    tagged = [e for e in events if e.get("tagged")]
    meetings = tagged or events
    total = len(meetings)

    from .billing import compute_amount

    amount, model = compute_amount(client_email, total)
    result = {
        "client_email": client_email,
        "period": period,
        "total_meetings": total,
        "billable_amount": amount,
        "pricing_model": model,
        "meetings": meetings,
    }
    print(
        f"{client_email}: {total} meetings / ${amount} ({model}) in last {days}d"
    )
    return result


def update_clients_meeting_count(client_email: str, total: int) -> None:
    from pipeline.to_sheets import connect
    import os

    gc = connect()
    sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
    ws = sh.worksheet("Clients")
    values = ws.get_all_values()
    headers = values[0]
    # ensure column
    if "Meetings" not in headers:
        ws.update_cell(1, len(headers) + 1, "Meetings")
        headers = ws.row_values(1)
    mcol = headers.index("Meetings") + 1
    ecol = headers.index("Email") + 1 if "Email" in headers else 3
    for i, row in enumerate(values[1:], start=2):
        if len(row) >= ecol and row[ecol - 1].strip().lower() == client_email.lower():
            ws.update_cell(i, mcol, str(total))
            print(f"Clients row {i}: Meetings={total}")
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track client meetings")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("count", help="Count billable meetings")
    p.add_argument("client_email")
    p.add_argument("--period", default="week", choices=["week", "month"])
    p.add_argument("--update-sheet", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "count":
        result = count_billable_meetings(args.client_email, period=args.period)
        if args.update_sheet:
            update_clients_meeting_count(
                args.client_email, result["total_meetings"]
            )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
