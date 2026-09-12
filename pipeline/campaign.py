"""First-touch email campaign against Pilot CRM / Agencies."""

from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from .email_sender import MAX_PER_RUN, send_batch
from .templates import build_agency_email, print_template_stats
from .to_sheets import HEADERS, get_all_values_safe, get_worksheet_safe, is_email, update_cell_safe

# Column letters / 1-based indexes matching HEADERS
COL = {name: i + 1 for i, name in enumerate(HEADERS)}
SENT_COL = COL["Sent"]  # G


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _pad(row: list[str], n: int | None = None) -> list[str]:
    width = n if n is not None else len(HEADERS)
    out = list(row) + [""] * width
    return out[:width]


def _first_name(contact: str) -> str:
    contact = (contact or "").strip()
    if not contact:
        return ""
    return contact.split()[0]


def get_unsent_leads() -> list[dict]:
    """Leads ready for first-touch: empty Sent, or 2-month-silent re-engage.

    Fresh unsent rows come first so the daily cap is not consumed only by
    stale re-engages. Re-engage rows keep ``Sent`` until a successful send
    (history is then copied into Notes — see ``pipeline.reengage``).
    """
    from .bounce_handler import check_recipient_sendable
    from .reengage import is_reengage_eligible

    ws = get_worksheet_safe("Agencies")
    values = get_all_values_safe(ws)
    fresh: list[dict] = []
    stale: list[dict] = []

    try:
        from .ai_reply_assistant import is_suppressed
    except Exception:
        is_suppressed = None

    for i, raw in enumerate(values):
        if i == 0:
            continue  # header
        row_index = i + 1  # 1-based sheet row
        cells = _pad(raw)
        fields = {name: cells[j] for j, name in enumerate(HEADERS)}

        email = fields["Email"].strip()
        verified = fields["Verified"].strip().lower()
        sent = fields["Sent"].strip()

        if not is_email(email):
            continue
        sendable, skip_reason = check_recipient_sendable(email)
        if not sendable:
            print(skip_reason)
            continue
        if is_suppressed is not None:
            try:
                if is_suppressed(email):
                    print(f"Skipped {email} (suppressed / do-not-contact)")
                    continue
            except Exception:
                pass

        reengage = False
        if sent:
            if not is_reengage_eligible(fields):
                continue
            reengage = True
        elif verified not in ("yes", "manual"):
            continue

        entry = {
            "row_index": row_index,
            "reengage": reengage,
            "sent_was": sent if reengage else "",
            **fields,
        }
        (stale if reengage else fresh).append(entry)

    if stale:
        print(f"reengage eligible on send list: {len(stale)}")
    return fresh + stale


def mark_sent(row_index: int, date_str: str) -> None:
    """Write YYYY-MM-DD into the Sent column for a sheet row."""
    ws = get_worksheet_safe("Agencies")
    update_cell_safe(ws, row_index, SENT_COL, date_str)


def _opener_from_lead(lead: dict) -> str:
    """Prefer a short H1/Page from scraped Signal; else generic opener."""
    from .personalize import parse_signal

    company = lead.get("Company", "").strip()
    raw = (lead.get("Signal") or "").strip()
    if raw:
        parts = parse_signal(raw)
        # Prefer a compact fact over the full Page|H1|Meta blob
        for key in ("H1", "Page", "Meta"):
            val = (parts.get(key) or "").strip()
            if val and len(val) > 8 and "just a moment" not in val.lower():
                return f"Noticed {company or 'your site'} highlights: {val[:160]}"
        if "Page:" not in raw:
            return raw[:200]
    return (
        f"Saw {company or 'your agency'} works with dental practices — "
        f"that's exactly the niche my outbound engine specializes in."
    )


def _signal_snippet(lead: dict) -> str:
    """Short signal for A9 template (default: your recent work)."""
    from .personalize import parse_signal

    raw = (lead.get("Signal") or "").strip()
    if not raw:
        return "your recent work"
    parts = parse_signal(raw)
    for key in ("H1", "Page", "Meta"):
        val = (parts.get(key) or "").strip()
        if val and len(val) > 8 and "just a moment" not in val.lower():
            # Keep subject line short
            words = val.split()
            return " ".join(words[:6])
    return "your recent work"


def _append_template_note(row_index: int, notes: str, template_id: str) -> None:
    marker = f"tpl={template_id}"
    if marker in (notes or ""):
        return
    new_notes = f"{(notes or '').rstrip()}; {marker}".strip("; ").strip()
    ws = get_worksheet_safe("Agencies")
    update_cell_safe(ws, row_index, COL["Notes"], new_notes)


def run_first_touch(limit: int = 5) -> dict:
    """Build and send first-touch emails for up to `limit` unsent leads.

    Scrapes website signals lazily — only for this send batch.
    """
    from .bounce_handler import scan_all_bounces
    from .personalize import ensure_signals_for_leads

    limit = max(1, min(int(limit), MAX_PER_RUN))
    leads = get_unsent_leads()
    batch_leads = leads[:limit]
    print(f"unsent leads: {len(leads)}; sending up to {limit}")

    if not batch_leads:
        print("nothing to send")
        return {"sent": [], "failed": []}

    # Lazy parallel scrape (skip rows that already have Signal / cache hit)
    ensure_signals_for_leads(batch_leads, concurrency=10, write_sheet=True)

    recipients: list[dict] = []
    for lead in batch_leads:
        company = lead.get("Company", "").strip()
        opener = _opener_from_lead(lead)
        first = _first_name(lead.get("Contact", ""))
        subject, body, template_id, selection_log = build_agency_email(
            first_name=first,
            company=company,
            opener=opener,
            signal=_signal_snippet(lead),
        )
        recipients.append(
            {
                "to": lead["Email"].strip(),
                "subject": subject,
                "body": body,
                "row_index": lead["row_index"],
                "Notes": lead.get("Notes", ""),
                "template_id": template_id,
                "template_selection_log": selection_log,
                "lead": lead,
                "reengage": bool(lead.get("reengage")),
                "sent_was": lead.get("sent_was") or "",
            }
        )

    from .send_queue import process_queue, queue_recipients, split_by_send_window

    queue_result = process_queue(limit)
    remaining = max(0, limit - len(queue_result.get("sent") or []))

    ready, to_queue = split_by_send_window(recipients)
    if to_queue:
        queued_count = queue_recipients(to_queue)
        print(f"timezone queue: {queued_count} deferred (outside 8-18 local / weekend)")
    recipients_to_send = ready[:remaining]

    result = send_batch(recipients_to_send, max_per_run=remaining or limit)
    sent_all = list(queue_result.get("sent") or []) + list(result.get("sent") or [])
    failed_all = list(queue_result.get("failed") or []) + list(result.get("failed") or [])
    result = {
        **result,
        "sent": sent_all,
        "failed": failed_all,
        "queued": len(to_queue),
        "queue_processed": len(queue_result.get("sent") or []),
    }
    today = date.today().isoformat()

    from .alerts import record_send_batch

    record_send_batch(len(result.get("sent") or []), len(result.get("failed") or []))

    if _dry_run():
        print(f"DRY_RUN: not marking Sent ({len(result['sent'])} would be marked {today})")
        reengaged = sum(1 for item in result["sent"] if item.get("reengage"))
        if reengaged:
            print(f"DRY_RUN: {reengaged} re-engage send(s) would get Notes sent_was/reengage_reset")
    else:
        from .reengage import append_reengage_notes

        for item in result["sent"]:
            row_index = item.get("row_index")
            if row_index:
                try:
                    notes = item.get("Notes", "")
                    if item.get("reengage"):
                        notes = append_reengage_notes(
                            notes, item.get("sent_was") or "", date.today()
                        )
                        ws = get_worksheet_safe("Agencies")
                        update_cell_safe(ws, row_index, COL["Notes"], notes)
                        item["Notes"] = notes
                    mark_sent(row_index, today)
                    tpl = item.get("template_id")
                    if tpl:
                        _append_template_note(row_index, notes, tpl)
                    kind = "reengage" if item.get("reengage") else "first-touch"
                    print(f"marked Sent={today} row {row_index} ({kind}, {tpl or 'no tpl'})")
                except Exception as e:
                    print(f"mark_sent failed row {row_index}: {e}")

    print(f"summary: sent {len(result['sent'])}, failed {len(result['failed'])}")
    if result.get("queued"):
        print(f"queued for later: {result['queued']}")
    if result.get("queue_processed"):
        print(f"sent from queue: {result['queue_processed']}")
    skipped = result.get("skipped_bounce") or []
    if skipped:
        print(f"skipped (bounced/blocked): {len(skipped)}")
    print_template_stats()
    try:
        scan_all_bounces(days=7)
    except Exception as e:
        print(f"bounce scan after send skipped: {e}")
    return result
