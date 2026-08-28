"""Calendly booking detection, Google Calendar sync, and meeting reminders."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "calendar_sync_state.json"

REMINDER_24H = timedelta(hours=24)
REMINDER_1H = timedelta(hours=1)
REMINDER_WINDOW = timedelta(minutes=20)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path, default: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def load_state() -> dict:
    return _load_json(STATE_PATH, {"seen_events": {}, "reminders_sent": {}})


def save_state(state: dict) -> None:
    state["updated_at"] = _utc_now().isoformat(timespec="seconds")
    _save_json(STATE_PATH, state)


def calendar_connected(client_email: str) -> bool:
    """True if client OAuth token exists and workspace is connected."""
    from delivery.connect_workspace import get_workspace_status
    from delivery.token_store import token_path

    email = client_email.strip().lower()
    status = (get_workspace_status(email) or "").lower()
    has_token = token_path(email).exists()
    legacy = ROOT / "tokens" / f"{email}.json"
    return has_token or legacy.exists() or status == "connected"


def _update_client_calendar_columns(
    client_email: str,
    *,
    calendar_connected_val: bool | None = None,
    meetings_this_month: int | None = None,
    next_meeting: str | None = None,
) -> None:
    from delivery.client_tracker import _clients_ws, _find_matches, ensure_tracker_columns
    from pipeline.to_sheets import get_all_values_safe, update_cell_safe

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = get_all_values_safe(ws)
    hits = _find_matches(values, cols, client_email.strip().lower(), "")
    if not hits:
        return
    row_index, _ = hits[-1]

    if calendar_connected_val is not None and "calendar_connected" in cols:
        update_cell_safe(
            ws, row_index, cols["calendar_connected"], "true" if calendar_connected_val else "false"
        )
    if meetings_this_month is not None and "meetings_this_month" in cols:
        update_cell_safe(ws, row_index, cols["meetings_this_month"], str(meetings_this_month))
    if next_meeting is not None and "next_meeting" in cols:
        update_cell_safe(ws, row_index, cols["next_meeting"], next_meeting)


def _booking_key(booking: dict) -> str:
    return (
        f"{booking.get('start_time')}|"
        f"{booking.get('attendee_email') or booking.get('practice_email')}|"
        f"{booking.get('practice_name')}"
    )


def create_calendar_event(client_email: str, booking: dict) -> dict:
    """Create Google Calendar event on client's primary calendar."""
    from googleapiclient.discovery import build

    from delivery.connect_workspace import refresh_tokens

    creds = refresh_tokens(client_email)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    practice = booking.get("practice_name") or booking.get("attendee_name") or "Dental practice"
    start = booking.get("start_time")
    end = booking.get("end_time")
    if not end and start:
        st = _parse_dt(start)
        if st:
            end = (st + timedelta(minutes=30)).isoformat()

    attendee = booking.get("attendee_email") or booking.get("practice_email") or ""
    description = (
        f"Practice: {practice}\n"
        f"Contact: {booking.get('attendee_name') or '—'}\n"
        f"Email: {attendee or '—'}\n"
        f"Source: DentalMeet Calendly booking\n"
        f"{booking.get('description') or ''}"
    ).strip()

    body = {
        "summary": f"Meeting with {practice}",
        "description": description,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "attendees": [{"email": client_email}],
    }
    if attendee:
        body["attendees"].append({"email": attendee})

    created = (
        service.events()
        .insert(calendarId="primary", body=body, sendUpdates="none")
        .execute()
    )
    print(f"calendar event created: {created.get('htmlLink') or created.get('id')}")
    return created


def _send_reminder_email(client_email: str, booking: dict, hours_before: int) -> bool:
    from pipeline.email_sender import send_email

    practice = booking.get("practice_name") or "the practice"
    when = (booking.get("start_time") or "")[:16].replace("T", " ")
    subject = f"Reminder: meeting with {practice} in {hours_before}h"
    body = (
        f"Hi,\n\n"
        f"Reminder — you have a meeting in about {hours_before} hour(s):\n\n"
        f"Practice: {practice}\n"
        f"Contact: {booking.get('attendee_name') or '—'}\n"
        f"Email: {booking.get('attendee_email') or '—'}\n"
        f"Time: {when} UTC\n"
        f"Topic: {booking.get('summary') or 'Dental practice discovery call'}\n\n"
        f"— DentalMeet\n"
    )
    return send_email(client_email, subject, body)


def _process_reminders(client_email: str, bookings: list[dict], state: dict) -> int:
    sent = 0
    now = _utc_now()
    reminders = state.setdefault("reminders_sent", {})
    client_rem = reminders.setdefault(client_email, {})

    for b in bookings:
        if (b.get("status") or "") not in ("scheduled", "confirmed"):
            continue
        start = _parse_dt(b.get("start_time"))
        if not start:
            continue
        key = _booking_key(b)
        delta = start - now
        for hours, label in ((24, "24h"), (1, "1h")):
            target = timedelta(hours=hours)
            if abs(delta - target) <= REMINDER_WINDOW:
                rk = f"{key}|{label}"
                if client_rem.get(rk):
                    continue
                if _send_reminder_email(client_email, b, hours):
                    client_rem[rk] = now.isoformat(timespec="seconds")
                    sent += 1
    return sent


def sync_client(client_email: str, *, dry_run: bool = False) -> dict:
    """Poll Calendly, create calendar events for new bookings, send reminders."""
    email = client_email.strip().lower()
    result = {
        "client_email": email,
        "calendar_connected": calendar_connected(email),
        "new_bookings": 0,
        "events_created": 0,
        "reminders_sent": 0,
        "skipped": False,
    }

    if not result["calendar_connected"]:
        print(f"skip {email}: calendar not connected")
        result["skipped"] = True
        _update_client_calendar_columns(email, calendar_connected_val=False)
        return result

    from delivery.track_meetings import get_calendly_events

    state = load_state()
    seen = state.setdefault("seen_events", {})
    client_seen = seen.setdefault(email, {})

    try:
        events = get_calendly_events(email, days=7)
    except Exception as e:
        print(f"Calendly poll failed for {email}: {e}")
        return result

    now = _utc_now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    meetings_this_month = sum(
        1
        for e in events
        if (e.get("status") or "") in ("scheduled", "completed")
        and _parse_dt(e.get("start_time"))
        and _parse_dt(e.get("start_time")) >= month_start
    )
    upcoming = [
        e
        for e in events
        if (e.get("status") or "") == "scheduled"
        and _parse_dt(e.get("start_time"))
        and _parse_dt(e.get("start_time")) > now
    ]
    upcoming.sort(key=lambda x: x.get("start_time") or "")
    next_meeting = (upcoming[0].get("start_time") or "") if upcoming else ""

    _update_client_calendar_columns(
        email,
        calendar_connected_val=True,
        meetings_this_month=meetings_this_month,
        next_meeting=next_meeting,
    )

    for ev in events:
        if (ev.get("status") or "") not in ("scheduled", "confirmed", "completed"):
            continue
        key = _booking_key(ev)
        if key in client_seen:
            continue
        result["new_bookings"] += 1
        if dry_run:
            print(f"would create event: {key}")
            client_seen[key] = {"at": now.isoformat(), "dry_run": True}
            continue
        try:
            create_calendar_event(
                email,
                {
                    "practice_name": ev.get("practice_name") or ev.get("attendee_company"),
                    "attendee_name": ev.get("attendee_name"),
                    "attendee_email": ev.get("attendee_email"),
                    "start_time": ev.get("start_time"),
                    "end_time": ev.get("end_time"),
                    "summary": ev.get("summary"),
                    "description": f"Booked via Calendly ({ev.get('source')})",
                },
            )
            client_seen[key] = {"at": now.isoformat(), "event": key}
            result["events_created"] += 1
        except Exception as e:
            print(f"calendar create failed for {email}: {e}")

    result["reminders_sent"] = _process_reminders(email, events, state)
    save_state(state)
    return result


def sync_all_active() -> None:
    from delivery.client_reports import active_clients

    for email in active_clients():
        try:
            sync_client(email)
        except Exception as e:
            print(f"sync failed {email}: {e}")


def simulate_booking(client_email: str, dry_run: bool = False) -> dict:
    """Simulate a Calendly booking and create a calendar event."""
    email = client_email.strip().lower()

    start = _utc_now() + timedelta(days=1)
    end = start + timedelta(minutes=30)
    booking = {
        "practice_name": "Smile Dental (test)",
        "attendee_name": "Dr. Jane Smith",
        "attendee_email": "dr.jane@smiledental.example",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "summary": "DentalMeet pilot discovery",
        "description": "Simulated Calendly booking for integration test",
    }

    if dry_run:
        print(f"DRY RUN: would create event for {email}: {booking}")
        return {"ok": True, "dry_run": True, "booking": booking}

    if not calendar_connected(email):
        print(f"ERROR: {email} calendar not connected — connect Workspace OAuth first")
        return {"ok": False, "reason": "not_connected"}

    created = create_calendar_event(email, booking)
    state = load_state()
    key = _booking_key(booking)
    state.setdefault("seen_events", {}).setdefault(email, {})[key] = {
        "at": _utc_now().isoformat(),
        "simulated": True,
    }
    save_state(state)
    _update_client_calendar_columns(
        email,
        calendar_connected_val=True,
        meetings_this_month=1,
        next_meeting=booking["start_time"],
    )
    return {"ok": True, "event_id": created.get("id"), "html_link": created.get("htmlLink")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calendly → Calendar sync + reminders")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync", help="Sync all active clients")

    p_one = sub.add_parser("sync_one", help="Sync one client")
    p_one.add_argument("client_email")
    p_one.add_argument("--dry-run", action="store_true")

    p_sim = sub.add_parser("simulate", help="Simulate a booking → calendar event")
    p_sim.add_argument("client_email")
    p_sim.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "sync":
        sync_all_active()
        return 0
    if args.cmd == "sync_one":
        sync_client(args.client_email, dry_run=args.dry_run)
        return 0
    if args.cmd == "simulate":
        r = simulate_booking(args.client_email, dry_run=args.dry_run)
        return 0 if r.get("ok") else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
