"""Semi-automatic reply intake via Gmail IMAP → Pilot CRM / Agencies."""

from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

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


def match_replies_to_leads(replies: list[dict]) -> dict:
    """Classify replies and route: onboarding / draft / payment / suppress."""
    from .ai_reply_assistant import (
        add_to_suppression,
        classify_incoming_email,
        draft_reply,
        is_suppressed,
        save_draft,
        send_payment_instructions,
    )
    from .async_onboarding import send_async_onboarding

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
            unmatched.append(addr)
            continue

        row_index = hit["row_index"]
        reply_type = classify_incoming_email(body)
        first = (hit.get("contact") or "").split()[0] if hit.get("contact") else ""
        today = datetime.now(timezone.utc).date().isoformat()
        action = ""

        try:
            update_cell_safe(ws,row_index, REPLIED_COL, "yes")
            update_cell_safe(ws,row_index, REPLY_TYPE_COL, reply_type)

            if reply_type == "unsubscribe":
                add_to_suppression(addr)
                notes = (hit.get("notes") or "").rstrip()
                marker = f"; unsubscribed {today}"
                update_cell_safe(ws,
                    row_index,
                    NOTES_COL,
                    f"{notes}{marker}" if notes else marker.lstrip("; ").strip(),
                )
                action = "suppressed"

            elif reply_type == "not_interested":
                action = "marked_not_interested"

            elif reply_type == "interested":
                already = "async_onboarding_sent" in (hit.get("meeting") or "")
                if not already:
                    if send_async_onboarding(addr, first, hit.get("company", "")):
                        update_cell_safe(ws,row_index, MEETING_COL, "async_onboarding_sent")
                        notes = (hit.get("notes") or "").rstrip()
                        marker = f"; async_onboarding_sent {today}"
                        update_cell_safe(ws,
                            row_index,
                            NOTES_COL,
                            f"{notes}{marker}" if notes else marker.lstrip("; ").strip(),
                        )
                        onboarded.append(addr)
                        action = "onboarding_sent"
                    else:
                        action = "onboarding_failed"
                else:
                    action = "onboarding_already_sent"

            elif reply_type == "payment_question":
                if send_payment_instructions(addr, first):
                    notes = (hit.get("notes") or "").rstrip()
                    marker = f"; payment_instructions_sent {today}"
                    update_cell_safe(ws,
                        row_index,
                        NOTES_COL,
                        f"{notes}{marker}" if notes else marker.lstrip("; ").strip(),
                    )
                    payments.append(addr)
                    action = "payment_sent"
                else:
                    action = "payment_failed"

            elif reply_type == "question":
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
                notes = (hit.get("notes") or "").rstrip()
                marker = f"; draft_saved {today} ({path.name})"
                update_cell_safe(ws,
                    row_index,
                    NOTES_COL,
                    f"{notes}{marker}" if notes else marker.lstrip("; ").strip(),
                )
                drafted.append(addr)
                action = f"draft:{path.name}"

            else:
                action = "logged"

            updated.append(
                {
                    "email": addr,
                    "row_index": row_index,
                    "reply_type": reply_type,
                    "subject": reply.get("subject", ""),
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
        "total_replies": len(replies),
    }


def run_reply_check(days: int = 3) -> dict:
    """Fetch recent replies and sync them into the sheet; print a report."""
    print(f"=== reply check (last {days} days) ===")
    replies = fetch_recent_replies(days=days)
    summary = match_replies_to_leads(replies)

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

    print("--- report ---")
    print(f"replies found: {summary['total_replies']}")
    print(f"leads updated: {len(summary['updated'])}")
    for u in summary["updated"]:
        print(
            f"  ✓ {u['email']} → type={u['reply_type']} "
            f"action={u.get('action', '')} (row {u['row_index']})"
        )
    if summary.get("onboarded"):
        print(f"async onboarding sent: {len(summary['onboarded'])}")
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
