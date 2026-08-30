"""Semi-automatic reply intake via Gmail IMAP → Pilot CRM / Agencies."""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .to_sheets import HEADERS, get_all_values_safe, get_worksheet_safe, is_email, update_cell_safe

COL = {name: i + 1 for i, name in enumerate(HEADERS)}
REPLIED_COL = COL["Replied"]
REPLY_TYPE_COL = COL["Reply type"]
MEETING_COL = COL["Meeting"]
NOTES_COL = COL["Notes"]
CONTACT_COL = COL["Contact"]
COMPANY_COL = COL["Company"]

SUBJECT_PREFIX = "re: meetings with dental practices"

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = ROOT / "data" / "response_templates.json"
DECLINE_PATH = ROOT / "data" / "decline_counts.json"
OOO_PATH = ROOT / "data" / "ooo_followups.json"
MANUAL_REVIEW_DIR = ROOT / "data" / "manual_review"


def _creds() -> tuple[str, str]:
    addr = os.environ.get("GMAIL_EMAIL", "").strip()
    # App passwords are often copied with spaces: "xxxx xxxx xxxx xxxx"
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if not addr or not password:
        raise RuntimeError("GMAIL_EMAIL and GMAIL_APP_PASSWORD must be set in .env")
    return addr, password


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_preview(msg: email.message.Message, limit: int = 300) -> str:
    """Extract plain-text preview (first `limit` chars)."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp.lower():
                try:
                    charset = part.get_content_charset() or "utf-8"
                    text = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    text = str(part.get_payload())
                break
        if not text:
            # Fall back to any text/html stripped roughly
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        charset = part.get_content_charset() or "utf-8"
                        html = part.get_payload(decode=True).decode(charset, errors="replace")
                        text = re.sub(r"<[^>]+>", " ", html)
                    except Exception:
                        pass
                    break
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            text = payload.decode(charset, errors="replace") if payload else str(msg.get_payload())
        except Exception:
            text = str(msg.get_payload())

    text = re.sub(r"\s+", " ", (text or "").strip())
    return text[:limit]


def _is_bounce_notification(msg: email.message.Message) -> bool:
    """True for delivery failure / mailer-daemon notifications."""
    _, from_email = parseaddr(msg.get("From", ""))
    from_email = (from_email or "").strip().lower()
    if "mailer-daemon" in from_email or "postmaster" in from_email:
        return True
    subject = _decode_header(msg.get("Subject")).strip().lower()
    return any(
        x in subject
        for x in ("undeliverable", "delivery status", "mail delivery failed", "returned mail")
    )


def _is_reply(msg: email.message.Message) -> bool:
    if msg.get("In-Reply-To"):
        return True
    subject = _decode_header(msg.get("Subject")).strip().lower()
    return subject.startswith(SUBJECT_PREFIX)


def fetch_recent_replies(days: int = 3) -> list[dict]:
    """Fetch recent inbox replies via Gmail IMAP (last N days)."""
    days = max(1, int(days))
    user, password = _creds()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")

    replies: list[dict] = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(user, password)
        mail.select("INBOX")

        # SINCE is inclusive at day granularity
        status, data = mail.search(None, f'(SINCE "{since}")')
        if status != "OK":
            print(f"IMAP search failed: {status}")
            return []

        ids = data[0].split() if data and data[0] else []
        print(f"IMAP: {len(ids)} messages since {since}")

        for num in ids:
            try:
                st, msg_data = mail.fetch(num, "(RFC822)")
                if st != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                if not _is_reply(msg) and not _is_bounce_notification(msg):
                    continue

                from_name, from_email = parseaddr(msg.get("From", ""))
                from_email = (from_email or "").strip().lower()
                if not is_email(from_email):
                    continue
                # Skip our own outbound copies
                if from_email == user.lower():
                    continue

                received = ""
                try:
                    dt = parsedate_to_datetime(msg.get("Date"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    received = dt.date().isoformat()
                except Exception:
                    received = ""

                body = _body_preview(msg, 2000)
                replies.append(
                    {
                        "from_email": from_email,
                        "from_name": _decode_header(from_name) or from_name,
                        "subject": _decode_header(msg.get("Subject")),
                        "body_preview": body[:300],
                        "body": body,
                        "received_date": received,
                    }
                )
            except Exception as e:
                print(f"IMAP parse error for msg {num}: {e}")
    except Exception as e:
        print(f"IMAP connection error: {e}")
        return []
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    print(f"IMAP: {len(replies)} reply candidates")
    return replies


def _is_bounce_notification_from_text(from_email: str, subject: str, body: str) -> bool:
    blob = f"{from_email} {subject} {body}".lower()
    if "mailer-daemon" in blob or "postmaster" in blob:
        return True
    return any(
        x in blob
        for x in ("undeliverable", "delivery status", "mail delivery failed", "returned mail")
    )


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, type(default)) else default
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_response_templates() -> dict:
    data = _load_json(TEMPLATES_PATH, {})
    return data if isinstance(data, dict) else {}


def send_classified_response(category: str, lead_email: str, first_name: str) -> bool:
    """Send presentation + form with category-specific messaging."""
    from .async_onboarding import brief_form_url, presentation_url
    from .email_sender import send_email

    templates = load_response_templates()
    tpl = templates.get(category)
    if not isinstance(tpl, dict):
        print(f"send_classified_response: missing template for {category!r}")
        return False

    name = (first_name or "").strip() or "there"
    email_addr = (lead_email or "").strip().lower()
    if not is_email(email_addr):
        return False

    subject = str(tpl.get("subject") or "DentalMeet")
    body = str(tpl.get("body") or "").format(
        name=name,
        presentation_url=presentation_url(),
        form_url=brief_form_url(),
    )
    ok = send_email(email_addr, subject, body)
    if ok:
        print(f"classified response ({category}) → {email_addr}")
    return ok


def _append_note(ws, row_index: int, existing: str, marker: str) -> str:
    notes = (existing or "").rstrip()
    new_notes = f"{notes}; {marker}" if notes else marker
    update_cell_safe(ws, row_index, NOTES_COL, new_notes)
    return new_notes


def record_decline(email_addr: str) -> int:
    """Increment not-interested count; return new total."""
    email_addr = (email_addr or "").strip().lower()
    counts = _load_json(DECLINE_PATH, {})
    if not isinstance(counts, dict):
        counts = {}
    n = int(counts.get(email_addr) or 0) + 1
    counts[email_addr] = n
    _save_json(DECLINE_PATH, counts)
    return n


def schedule_ooo_followup(
    email_addr: str,
    first_name: str,
    company: str,
    days: int = 7,
) -> str:
    """Queue presentation send for N days later. Returns follow-up date ISO."""
    email_addr = (email_addr or "").strip().lower()
    follow_date = (datetime.now(timezone.utc).date() + timedelta(days=max(1, days))).isoformat()
    items = _load_json(OOO_PATH, [])
    if not isinstance(items, list):
        items = []
    # Replace existing pending for same email
    items = [
        x
        for x in items
        if not (
            isinstance(x, dict)
            and (x.get("email") or "").lower() == email_addr
            and not x.get("sent")
        )
    ]
    items.append(
        {
            "email": email_addr,
            "first_name": first_name or "",
            "company": company or "",
            "follow_up_date": follow_date,
            "created": datetime.now(timezone.utc).date().isoformat(),
            "sent": False,
        }
    )
    _save_json(OOO_PATH, items)
    print(f"Recipient out of office, will follow up on {follow_date}")
    return follow_date


def process_due_ooo_followups() -> list[str]:
    """Send soft presentation pack for due OOO follow-ups."""
    today = datetime.now(timezone.utc).date().isoformat()
    items = _load_json(OOO_PATH, [])
    if not isinstance(items, list):
        return []
    sent_list: list[str] = []
    changed = False
    for item in items:
        if not isinstance(item, dict) or item.get("sent"):
            continue
        if (item.get("follow_up_date") or "") > today:
            continue
        email_addr = (item.get("email") or "").strip().lower()
        if not is_email(email_addr):
            item["sent"] = True
            changed = True
            continue
        from .ai_reply_assistant import is_suppressed

        if is_suppressed(email_addr):
            item["sent"] = True
            changed = True
            continue
        # Soft reopen after OOO — use not_interested soft template tone is wrong;
        # use question (helpful, non-pushy) pack.
        ok = send_classified_response(
            "question",
            email_addr,
            item.get("first_name") or "",
        )
        if ok:
            item["sent"] = True
            item["sent_date"] = today
            sent_list.append(email_addr)
            changed = True
            print(f"OOO follow-up sent → {email_addr}")
    if changed:
        _save_json(OOO_PATH, items)
    return sent_list


def forward_to_manual_review(reply: dict, hit: dict | None) -> Path:
    """Persist ambiguous replies for human review."""
    MANUAL_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    addr = (reply.get("from_email") or "unknown").strip().lower()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^a-z0-9._+-]+", "_", addr)[:80]
    path = MANUAL_REVIEW_DIR / f"{stamp}_{safe}.json"
    payload = {
        "email": addr,
        "subject": reply.get("subject", ""),
        "body": reply.get("body") or reply.get("body_preview") or "",
        "received_date": reply.get("received_date", ""),
        "lead": {
            "contact": (hit or {}).get("contact", ""),
            "company": (hit or {}).get("company", ""),
            "row_index": (hit or {}).get("row_index"),
        },
        "reason": "ambiguous_or_other",
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"manual review queued → {path.name}")
    return path


def _already_sent_response(hit: dict) -> bool:
    blob = f"{hit.get('meeting') or ''} {hit.get('notes') or ''}".lower()
    return "response_sent" in blob or "async_onboarding_sent" in blob


def match_replies_to_leads(replies: list[dict]) -> dict:
    """Classify replies and route: presentation pack / OOO / bounce / review."""
    from .ai_reply_assistant import (
        add_to_do_not_contact,
        add_to_suppression,
        is_suppressed,
        save_draft,
        send_payment_instructions,
        draft_reply,
    )
    from .reply_classifier import classify_reply

    ws = get_worksheet_safe("Agencies")
    values = get_all_values_safe(ws)

    by_email: dict[str, dict] = {}
    for i, raw in enumerate(values):
        if i == 0:
            continue
        row = (list(raw) + [""] * len(HEADERS))[: len(HEADERS)]
        email_addr = row[COL["Email"] - 1].strip().lower()
        if not is_email(email_addr):
            continue
        by_email[email_addr] = {
            "row_index": i + 1,
            "company": row[COMPANY_COL - 1].strip(),
            "contact": row[CONTACT_COL - 1].strip(),
            "title": row[COL["Title"] - 1].strip(),
            "email": email_addr,
            "replied": row[REPLIED_COL - 1].strip(),
            "reply_type": row[REPLY_TYPE_COL - 1].strip(),
            "meeting": row[MEETING_COL - 1].strip(),
            "notes": row[NOTES_COL - 1],
        }

    updated: list[dict] = []
    unmatched: list[str] = []
    skipped: list[str] = []
    onboarded: list[str] = []
    drafted: list[str] = []
    payments: list[str] = []
    responses_sent: list[str] = []
    manual_review: list[str] = []
    ooo_scheduled: list[str] = []

    seen: set[str] = set()
    for reply in replies:
        addr = (reply.get("from_email") or "").strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)

        body = reply.get("body") or reply.get("body_preview") or ""
        if _is_bounce_notification_from_text(addr, reply.get("subject", ""), body):
            from .bounce_handler import extract_bounced_recipient, record_bounced_email

            recipient = extract_bounced_recipient(
                addr, reply.get("subject", ""), body
            ) or addr
            if record_bounced_email(recipient, source="gmail_replies"):
                pass
            skipped.append(addr)
            print(f"bounce detected: {recipient}")
            continue

        if is_suppressed(addr):
            print(f"skip suppressed: {addr}")
            skipped.append(addr)
            continue

        hit = by_email.get(addr)
        if not hit:
            # Still classify unmatched for manual review if not bounce
            classification = classify_reply(body)
            if classification.get("category") == "other":
                forward_to_manual_review(reply, None)
                manual_review.append(addr)
            unmatched.append(addr)
            continue

        row_index = hit["row_index"]
        classification = classify_reply(body)
        reply_type = classification.get("category") or "other"
        first = (hit.get("contact") or "").split()[0] if hit.get("contact") else ""
        today = datetime.now(timezone.utc).date().isoformat()
        action = ""

        try:
            update_cell_safe(ws, row_index, REPLIED_COL, "yes")
            update_cell_safe(ws, row_index, REPLY_TYPE_COL, reply_type)

            if reply_type == "unsubscribe":
                add_to_suppression(addr)
                hit["notes"] = _append_note(ws, row_index, hit.get("notes") or "", f"unsubscribed {today}")
                action = "suppressed"

            elif reply_type == "bounce":
                from .bounce_handler import record_bounced_email

                record_bounced_email(addr, source="reply_classify")
                action = "bounced"

            elif reply_type == "out_of_office":
                follow_date = schedule_ooo_followup(
                    addr, first, hit.get("company", ""), days=7
                )
                hit["notes"] = _append_note(
                    ws,
                    row_index,
                    hit.get("notes") or "",
                    f"ooo_followup_{follow_date}",
                )
                ooo_scheduled.append(addr)
                action = f"ooo_followup:{follow_date}"

            elif reply_type in ("interested", "question", "not_interested"):
                already = _already_sent_response(hit)
                if not already:
                    if send_classified_response(reply_type, addr, first):
                        update_cell_safe(ws, row_index, MEETING_COL, "response_sent")
                        hit["notes"] = _append_note(
                            ws,
                            row_index,
                            hit.get("notes") or "",
                            f"response_sent {reply_type} {today}",
                        )
                        responses_sent.append(addr)
                        onboarded.append(addr)
                        action = f"response_sent:{reply_type}"
                    else:
                        action = "response_failed"
                else:
                    action = "response_already_sent"

                if reply_type == "not_interested":
                    declines = record_decline(addr)
                    if declines >= 2:
                        add_to_do_not_contact(addr)
                        hit["notes"] = _append_note(
                            ws,
                            row_index,
                            hit.get("notes") or "",
                            f"do_not_contact {today}",
                        )
                        action = f"{action};dnc"
                    else:
                        action = f"{action};decline:{declines}"

                # Keep AI draft for questions as backup for human polish
                if reply_type == "question":
                    draft = draft_reply(
                        body,
                        {
                            "contact": hit.get("contact", ""),
                            "company": hit.get("company", ""),
                            "email": addr,
                            "title": hit.get("title", ""),
                        },
                    )
                    path = save_draft(addr, body, draft, hit)
                    hit["notes"] = _append_note(
                        ws,
                        row_index,
                        hit.get("notes") or "",
                        f"draft_saved {today} ({path.name})",
                    )
                    drafted.append(addr)

            elif reply_type == "payment_question":
                if send_payment_instructions(addr, first):
                    hit["notes"] = _append_note(
                        ws,
                        row_index,
                        hit.get("notes") or "",
                        f"payment_instructions_sent {today}",
                    )
                    payments.append(addr)
                    action = "payment_sent"
                else:
                    action = "payment_failed"

            elif reply_type == "other":
                path = forward_to_manual_review(reply, hit)
                hit["notes"] = _append_note(
                    ws,
                    row_index,
                    hit.get("notes") or "",
                    f"manual_review {today} ({path.name})",
                )
                manual_review.append(addr)
                action = f"manual_review:{path.name}"

            else:
                action = "logged"

            updated.append(
                {
                    "email": addr,
                    "row_index": row_index,
                    "reply_type": reply_type,
                    "language": classification.get("language"),
                    "subject": reply.get("subject", ""),
                    "notes": hit.get("notes") or "",
                    "previous_replied": hit["replied"],
                    "previous_type": hit["reply_type"],
                    "onboarding_sent": addr in onboarded,
                    "action": action,
                }
            )
        except Exception as e:
            print(f"sheet update failed for {addr} row {row_index}: {e}")
            skipped.append(addr)

    return {
        "updated": updated,
        "unmatched": unmatched,
        "skipped": skipped,
        "onboarded": onboarded,
        "drafted": drafted,
        "payments": payments,
        "responses_sent": responses_sent,
        "manual_review": manual_review,
        "ooo_scheduled": ooo_scheduled,
        "total_replies": len(replies),
    }


def run_reply_check(days: int = 3) -> dict:
    """Fetch recent replies and sync them into the sheet; print a report."""
    print(f"=== reply check (last {days} days) ===")
    ooo_sent = process_due_ooo_followups()
    if ooo_sent:
        print(f"OOO follow-ups sent: {len(ooo_sent)}")

    replies = fetch_recent_replies(days=days)
    summary = match_replies_to_leads(replies)
    summary["ooo_followups_sent"] = ooo_sent

    from .alerts import bump_daily_metric

    bump_daily_metric("replies", summary.get("total_replies") or 0)
    for u in summary.get("updated") or []:
        rt = u.get("reply_type")
        if rt == "interested":
            bump_daily_metric("interested")
            bump_daily_metric("replies_interested")
        elif rt == "question":
            bump_daily_metric("replies_questions")
        elif rt == "not_interested":
            bump_daily_metric("replies_not_interested")

    from .template_ab import record_replied, template_id_from_notes

    for u in summary.get("updated") or []:
        tpl = template_id_from_notes(u.get("notes") or "")
        if tpl:
            record_replied(tpl, interested=u.get("reply_type") == "interested")

    print("--- report ---")
    print(f"replies found: {summary['total_replies']}")
    print(f"leads updated: {len(summary['updated'])}")
    for u in summary["updated"]:
        lang = u.get("language") or "?"
        print(
            f"  ✓ {u['email']} → type={u['reply_type']} lang={lang} "
            f"action={u.get('action', '')} (row {u['row_index']})"
        )
    if summary.get("responses_sent"):
        print(f"presentation+form responses sent: {len(summary['responses_sent'])}")
    if summary.get("onboarded"):
        print(f"async onboarding / classified packs: {len(summary['onboarded'])}")
    if summary.get("ooo_scheduled"):
        print(f"OOO follow-ups scheduled: {len(summary['ooo_scheduled'])}")
    if summary.get("manual_review"):
        print(f"manual review: {len(summary['manual_review'])} → data/manual_review/")
    if summary.get("drafted"):
        print(f"drafts saved: {len(summary['drafted'])} → data/drafts/")
    if summary.get("payments"):
        print(f"payment instructions sent: {len(summary['payments'])}")
    if summary["unmatched"]:
        print(f"unmatched senders ({len(summary['unmatched'])}):")
        for addr in summary["unmatched"]:
            print(f"  ? {addr}")
    if summary["skipped"]:
        print(f"skipped: {summary['skipped']}")
    print("=== end reply check ===")
    return summary
