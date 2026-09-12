"""Queue outbound emails until recipient-local send windows open."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .timezone_utils import (
    DEFAULT_TZ,
    can_send_now,
    format_local_datetime_safe,
    get_recipient_timezone,
    next_send_datetime,
    recipient_local_time,
    tz_abbrev,
)

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "data" / "send_queue.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_queue() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("items") or [])
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_queue(items: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(
        json.dumps({"items": items}, indent=2) + "\n",
        encoding="utf-8",
    )


def enqueue_item(
    item: dict,
    *,
    tz_name: str,
    send_after: datetime,
    lead: dict | None = None,
) -> dict:
    """Add email to send queue."""
    queue = load_queue()
    entry = {
        "id": str(uuid.uuid4()),
        "to": item.get("to", ""),
        "subject": item.get("subject", ""),
        "body": item.get("body", ""),
        "row_index": item.get("row_index"),
        "Notes": item.get("Notes", ""),
        "template_id": item.get("template_id", ""),
        "template_selection_log": item.get("template_selection_log", ""),
        "reengage": bool(item.get("reengage")),
        "sent_was": item.get("sent_was") or "",
        "from_email": item.get("from_email") or "",
        "timezone": tz_name,
        "queued_at": _utc_now().isoformat(timespec="seconds"),
        "send_after": send_after.astimezone(timezone.utc).isoformat(),
        "lead": lead or item.get("lead") or {},
    }
    queue.append(entry)
    save_queue(queue)
    when = format_local_datetime_safe(send_after, tz_name)
    print(f"queued {entry['to']} for {when}")
    return entry


def remove_items(item_ids: set[str]) -> None:
    if not item_ids:
        return
    queue = [q for q in load_queue() if q.get("id") not in item_ids]
    save_queue(queue)


def get_due_items(now: datetime | None = None) -> list[dict]:
    """Return queue items whose send_after has passed."""
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    due: list[dict] = []
    for item in load_queue():
        raw = item.get("send_after") or ""
        try:
            send_after = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if send_after.tzinfo is None:
                send_after = send_after.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if send_after <= now:
            due.append(item)
    return due


def get_ready_to_send(now: datetime | None = None) -> list[dict]:
    """Due items that are also inside recipient send window (weekday 8-18)."""
    ready: list[dict] = []
    for item in get_due_items(now):
        tz_name = item.get("timezone") or get_recipient_timezone(item.get("lead") or {})
        if can_send_now(tz_name, now):
            ready.append(item)
    return ready


def split_by_send_window(recipients: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split recipients into ready-to-send vs should-queue.
    Mutates nothing; queue side includes computed metadata.
    """
    ready: list[dict] = []
    to_queue: list[dict] = []
    for item in recipients:
        lead = item.get("lead") or item
        tz_name = get_recipient_timezone(lead)
        local = recipient_local_time(tz_name)
        item = {**item, "timezone": tz_name}
        if can_send_now(tz_name, local):
            ready.append(item)
        else:
            send_after = next_send_datetime(tz_name, local)
            to_queue.append({**item, "send_after": send_after, "timezone": tz_name})
    return ready, to_queue


def process_queue(limit: int) -> dict:
    """Send due queued emails that are in window; remove from queue on success."""
    from .email_sender import send_batch

    ready = get_ready_to_send()[:limit]
    if not ready:
        return {"sent": [], "failed": [], "processed": 0}

    batch = [
        {
            "to": q["to"],
            "subject": q["subject"],
            "body": q["body"],
            "row_index": q.get("row_index"),
            "Notes": q.get("Notes", ""),
            "template_id": q.get("template_id", ""),
            "template_selection_log": q.get("template_selection_log", ""),
            "reengage": bool(q.get("reengage")),
            "sent_was": q.get("sent_was") or "",
            "from_email": q.get("from_email") or "",
            "timezone": q.get("timezone"),
            "queue_id": q.get("id"),
        }
        for q in ready
    ]
    result = send_batch(batch, max_per_run=limit)
    sent_ids = {item.get("queue_id") for item in result.get("sent") or [] if item.get("queue_id")}
    remove_items({i for i in sent_ids if i})
    print(f"queue processed: {len(result.get('sent') or [])} sent")
    return {
        **result,
        "processed": len(ready),
        "sent_ids": list(sent_ids),
    }


def print_queue_status() -> None:
    """CLI summary: e.g. '15 emails queued for tomorrow 9:00 PT'."""
    queue = load_queue()
    if not queue:
        print("Send queue empty.")
        return

    print(f"=== SEND QUEUE ({len(queue)} emails) ===")
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in queue:
        tz_name = item.get("timezone") or get_recipient_timezone(item.get("lead") or {})
        raw = item.get("send_after") or ""
        try:
            send_after = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            send_after = next_send_datetime(tz_name)
        label = format_local_datetime_safe(send_after, tz_name)
        groups[label].append(item)

    for label, items in sorted(groups.items(), key=lambda x: x[0]):
        abbrevs = {tz_abbrev(i.get("timezone") or DEFAULT_TZ) for i in items}
        tz_label = next(iter(abbrevs)) if len(abbrevs) == 1 else "mixed TZ"
        print(f"  {len(items)} emails queued for {label}")
        for item in items[:3]:
            print(f"    • {item.get('to')}")
        if len(items) > 3:
            print(f"    … and {len(items) - 3} more")
    print("======================================")


def queue_recipients(to_queue: list[dict]) -> int:
    """Persist recipients outside send window. Returns count queued."""
    count = 0
    for item in to_queue:
        tz_name = item.get("timezone") or get_recipient_timezone(item.get("lead") or item)
        send_after = item.get("send_after") or next_send_datetime(
            tz_name, recipient_local_time(tz_name)
        )
        enqueue_item(item, tz_name=tz_name, send_after=send_after, lead=item.get("lead"))
        count += 1
    return count
