"""Reply intake via IMAP across all sending accounts → Pilot CRM / Agencies.

Reads Zoho (dentalmeet.agency) and Google Workspace (meet-agency.com) inboxes,
plus optional legacy GMAIL_EMAIL ops inbox. Warmup traffic is excluded.
"""

from __future__ import annotations

import email
import hashlib
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
PROCESSED_PATH = ROOT / "data" / "processed_replies.json"
INBOX_FAIL_PATH = ROOT / "data" / "reply_inbox_failures.json"
MAX_PROCESSED_IDS = 5000
FAIL_STREAK_ALERT = 3


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
    if msg.get("In-Reply-To") or msg.get("References"):
        return True
    subject = _decode_header(msg.get("Subject")).strip().lower()
    return subject.startswith(SUBJECT_PREFIX) or subject.startswith("re:")


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


def _processed_ids() -> set[str]:
    data = _load_json(PROCESSED_PATH, {"ids": []})
    ids = data.get("ids") if isinstance(data, dict) else []
    return {str(x) for x in (ids or []) if x}


def _mark_processed(message_ids: list[str]) -> None:
    if not message_ids:
        return
    data = _load_json(PROCESSED_PATH, {"ids": []})
    if not isinstance(data, dict):
        data = {"ids": []}
    existing = [str(x) for x in (data.get("ids") or []) if x]
    seen = set(existing)
    for mid in message_ids:
        if mid and mid not in seen:
            existing.append(mid)
            seen.add(mid)
    data["ids"] = existing[-MAX_PROCESSED_IDS:]
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_json(PROCESSED_PATH, data)


def _message_fingerprint(
    *,
    message_id: str,
    from_email: str,
    subject: str,
    body: str,
    received_at: str,
) -> str:
    mid = (message_id or "").strip()
    if mid:
        return mid
    blob = f"{from_email}|{subject}|{body[:500]}|{received_at}"
    return "hash:" + hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def _warmup_addrs() -> set[str]:
    from .smtp_accounts import own_account_emails

    addrs = set(own_account_emails())
    gmail = os.environ.get("GMAIL_EMAIL", "").strip().lower()
    if gmail:
        addrs.add(gmail)
    try:
        from .warmup import load_external_recipients

        for row in load_external_recipients():
            e = (row.get("email") or "").strip().lower()
            if e:
                addrs.add(e)
    except Exception:
        pass
    return addrs


def _is_warmup_traffic(from_email: str, to_email: str, account_email: str) -> bool:
    warm = _warmup_addrs()
    fr = (from_email or "").strip().lower()
    to = (to_email or "").strip().lower()
    acc = (account_email or "").strip().lower()
    if fr in warm:
        return True
    # Internal peer mail: both ends are our accounts
    if fr in warm and (to in warm or acc in warm):
        return True
    return False


def _extract_to_email(msg: email.message.Message, account_email: str) -> str:
    for header in ("Delivered-To", "X-Original-To", "To"):
        raw = msg.get(header)
        if not raw:
            continue
        # To: can be a list of addresses
        parts = email.utils.getaddresses([raw])
        for _, addr in parts:
            a = (addr or "").strip().lower()
            if is_email(a):
                return a
    return (account_email or "").strip().lower()


def _parse_message(
    msg: email.message.Message,
    *,
    account_email: str,
) -> dict | None:
    from_name, from_email = parseaddr(msg.get("From", ""))
    from_email = (from_email or "").strip().lower()
    if not is_email(from_email) and not _is_bounce_notification(msg):
        return None
    if from_email == (account_email or "").strip().lower():
        return None

    to_email = _extract_to_email(msg, account_email)
    received_at = ""
    received_date = ""
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        received_at = dt.isoformat()
        received_date = dt.date().isoformat()
    except Exception:
        received_at = datetime.now(timezone.utc).isoformat()
        received_date = datetime.now(timezone.utc).date().isoformat()

    body = _body_preview(msg, 2000)
    subject = _decode_header(msg.get("Subject"))
    message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    references = (msg.get("References") or "").strip()
    fingerprint = _message_fingerprint(
        message_id=message_id,
        from_email=from_email,
        subject=subject,
        body=body,
        received_at=received_at,
    )

    is_warmup = _is_warmup_traffic(from_email, to_email, account_email)
    return {
        "from_email": from_email,
        "from_name": _decode_header(from_name) or from_name,
        "to_email": to_email,
        "account_email": (account_email or "").strip().lower(),
        "subject": subject,
        "body_preview": body[:300],
        "body": body,
        "message_id": message_id or fingerprint,
        "fingerprint": fingerprint,
        "in_reply_to": in_reply_to,
        "references": references,
        "received_at": received_at,
        "received_date": received_date,
        "is_warmup": is_warmup,
        "is_bounce": _is_bounce_notification(msg),
    }


def _imap_connect(acc: dict) -> imaplib.IMAP4_SSL:
    host = str(acc.get("imap_server") or "imap.zoho.com")
    port = int(acc.get("imap_port") or 993)
    user = acc["email"]
    password = acc["password"]
    mail = imaplib.IMAP4_SSL(host, port)
    mail.login(user, password)
    return mail


def _record_inbox_success(email_addr: str) -> None:
    data = _load_json(INBOX_FAIL_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data[email_addr.lower()] = {
        "consecutive_failures": 0,
        "last_ok": datetime.now(timezone.utc).isoformat(),
        "last_error": "",
    }
    _save_json(INBOX_FAIL_PATH, data)


def _record_inbox_failure(email_addr: str, error: str) -> None:
    data = _load_json(INBOX_FAIL_PATH, {})
    if not isinstance(data, dict):
        data = {}
    key = email_addr.lower()
    prev = data.get(key) if isinstance(data.get(key), dict) else {}
    streak = int(prev.get("consecutive_failures") or 0) + 1
    data[key] = {
        "consecutive_failures": streak,
        "last_error": str(error)[:500],
        "last_fail": datetime.now(timezone.utc).isoformat(),
        "last_ok": prev.get("last_ok") or "",
    }
    _save_json(INBOX_FAIL_PATH, data)
    if streak >= FAIL_STREAK_ALERT:
        try:
            from .alerts import send_alert

            send_alert(
                f"Reply IMAP failing: {email_addr}",
                f"{email_addr} failed IMAP {streak} runs in a row.\n\nLast error:\n{error}",
                severity="warning",
            )
        except Exception as e:
            print(f"inbox failure alert skipped: {e}")


def fetch_inbox_replies(
    acc: dict,
    *,
    days: int = 3,
    processed: set[str] | None = None,
) -> dict:
    """
    Fetch reply candidates from one account inbox.
    Returns {account, connected, error, messages, new, lead_candidates, warmup_skipped}.
    """
    days = max(1, int(days))
    account_email = acc["email"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    processed = processed if processed is not None else _processed_ids()
    result = {
        "account": account_email,
        "provider": acc.get("provider") or "?",
        "connected": False,
        "error": "",
        "messages": 0,
        "new": 0,
        "lead_candidates": [],
        "warmup_skipped": 0,
        "warmup_fingerprints": [],
        "already_processed": 0,
    }
    mail = None
    try:
        mail = _imap_connect(acc)
        mail.select("INBOX")
        result["connected"] = True
        status, data = mail.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split() if data and data[0] else []
        result["messages"] = len(ids)

        for num in ids:
            try:
                st, msg_data = mail.fetch(num, "(RFC822)")
                if st != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                if not _is_reply(msg) and not _is_bounce_notification(msg):
                    continue
                parsed = _parse_message(msg, account_email=account_email)
                if not parsed:
                    continue
                fp = parsed["fingerprint"]
                if fp in processed:
                    result["already_processed"] += 1
                    continue
                result["new"] += 1
                if parsed.get("is_warmup") and not parsed.get("is_bounce"):
                    result["warmup_skipped"] += 1
                    result.setdefault("warmup_fingerprints", []).append(fp)
                    processed.add(fp)
                    continue
                result["lead_candidates"].append(parsed)
                # Do not mark lead fps until CRM actions succeed
            except Exception as e:
                print(f"IMAP parse error {account_email} msg {num}: {e}")

        _record_inbox_success(account_email)
    except Exception as e:
        result["error"] = str(e)
        print(f"IMAP error for {account_email}: {e}")
        _record_inbox_failure(account_email, str(e))
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    return result


def fetch_recent_replies(days: int = 3) -> list[dict]:
    """Fetch recent replies from ALL enabled sending-account IMAP inboxes."""
    from .smtp_accounts import load_reply_imap_accounts

    accounts = load_reply_imap_accounts(with_password=True)
    if not accounts:
        print(
            "fetch_recent_replies: no IMAP accounts with passwords "
            "(set SMTP_* / GOOGLE_SMTP_* or per-account password_env)"
        )
        return []

    processed = _processed_ids()
    all_replies: list[dict] = []
    warmup_fps: list[str] = []
    print(f"IMAP: scanning {len(accounts)} inbox(es) (last {days} days)")

    for acc in accounts:
        info = fetch_inbox_replies(acc, days=days, processed=processed)
        leads = info.get("lead_candidates") or []
        warmup_fps.extend(info.get("warmup_fingerprints") or [])
        status = "connected" if info["connected"] else f"FAILED ({info['error']})"
        print(
            f"  {info['account']}: {status}, "
            f"{info['new']} new, {len(leads)} lead reply candidates "
            f"(warmup skipped={info['warmup_skipped']})"
        )
        all_replies.extend(leads)

    if warmup_fps:
        _mark_processed(warmup_fps)

    print(f"IMAP: {len(all_replies)} lead reply candidates across all inboxes")
    return all_replies


def diagnose_reply_inboxes(days: int = 3) -> list[dict]:
    """Connect to each reply inbox and print a short status report."""
    from .smtp_accounts import load_reply_imap_accounts

    accounts = load_reply_imap_accounts(with_password=True)
    processed = _processed_ids()
    rows: list[dict] = []
    print("=== REPLY INBOXES ===")
    if not accounts:
        print("(no IMAP accounts with passwords configured)")
        print("=====================")
        return rows

    for acc in accounts:
        # Copy processed set so diagnostics do not mark lead message IDs
        info = fetch_inbox_replies(acc, days=days, processed=set(processed))
        leads = len(info.get("lead_candidates") or [])
        if info["connected"]:
            line = (
                f"{info['account']}: connected, "
                f"{info['new']} new messages, {leads} lead "
                f"{'reply' if leads == 1 else 'replies'}"
            )
            if info["warmup_skipped"]:
                line += f" (warmup skipped={info['warmup_skipped']})"
        else:
            line = f"{info['account']}: FAILED — {info['error']}"
        print(line)
        rows.append(info)
    print("=====================")
    return rows


def _is_bounce_notification_from_text(from_email: str, subject: str, body: str) -> bool:
    blob = f"{from_email} {subject} {body}".lower()
    if "mailer-daemon" in blob or "postmaster" in blob:
        return True
    return any(
        x in blob
        for x in ("undeliverable", "delivery status", "mail delivery failed", "returned mail")
    )


def load_response_templates() -> dict:
    data = _load_json(TEMPLATES_PATH, {})
    return data if isinstance(data, dict) else {}


def _default_subject(original_subject: str) -> str:
    subj = (original_subject or "").strip()
    if not subj:
        return "DentalMeet"
    if subj.lower().startswith("re:"):
        return subj
    return f"Re: {subj}"


def render_response_template(
    category: str,
    *,
    name: str = "there",
    original_subject: str = "",
    presentation_url: str = "",
    form_url: str = "",
) -> dict[str, str] | None:
    """Return {subject, body} for a category, or None if missing."""
    templates = load_response_templates()
    tpl = templates.get(category)
    if not isinstance(tpl, dict):
        return None
    name = (name or "").strip() or "there"
    orig = (original_subject or "").strip() or "your note"
    preso = presentation_url or ""
    form = form_url or ""
    fmt = {
        "name": name,
        "original_subject": orig,
        "presentation_url": preso,
        "presentation_link": preso,
        "form_url": form,
        "form_link": form,
    }
    try:
        subject = str(tpl.get("subject") or "Re: {original_subject}").format(**fmt)
    except (KeyError, ValueError):
        subject = _default_subject(orig)
    # Guard empty / broken subject after format
    if "{original_subject}" in subject:
        subject = _default_subject(orig)
    try:
        body = str(tpl.get("body") or "").format(**fmt)
    except (KeyError, ValueError) as e:
        print(f"template format error ({category}): {e}")
        return None
    return {"subject": subject, "body": body}


def print_response_templates() -> None:
    """CLI preview of all auto-reply templates."""
    from .async_onboarding import brief_form_url, presentation_url

    templates = load_response_templates()
    print("=== AUTO-REPLY TEMPLATES ===")
    if not templates:
        print("(no templates in data/response_templates.json)")
        print("===========================")
        return
    preso = presentation_url()
    form = brief_form_url()
    banned = (
        "hop on a call",
        "schedule a time",
        "book a meeting",
        "quick call",
        "15-min",
        "15 min",
        "let's talk",
        "lets talk",
        "available for a call",
        "calendly",
        "zoom",
        "book a call",
        "on a call",
    )
    for category in (
        "interested",
        "question",
        "not_interested",
        "out_of_office",
        "other",
    ):
        rendered = render_response_template(
            category,
            name="Alex",
            original_subject="meetings with dental practices",
            presentation_url=preso,
            form_url=form,
        )
        print(f"\n--- {category} ---")
        if not rendered:
            print("(missing)")
            continue
        subject = rendered["subject"]
        body = rendered["body"]
        try:
            print(f"SUBJECT: {subject}")
            print(body)
        except UnicodeEncodeError:
            print(f"SUBJECT: {subject}".encode("ascii", "replace").decode("ascii"))
            print(body.encode("ascii", "replace").decode("ascii"))
        low = rendered["body"].lower()
        hits = [b for b in banned if b in low]
        if hits:
            print(f"WARN CALL/MEETING PHRASES FOUND: {hits}")
        else:
            print("OK no call/scheduling phrases")
    print("\n===========================")


def send_classified_response(
    category: str,
    lead_email: str,
    first_name: str,
    *,
    original_subject: str = "",
) -> bool:
    """Send presentation + form with category-specific messaging (email-only)."""
    from .async_onboarding import brief_form_url, presentation_url
    from .email_sender import send_email

    email_addr = (lead_email or "").strip().lower()
    if not is_email(email_addr):
        return False

    rendered = render_response_template(
        category,
        name=(first_name or "").strip() or "there",
        original_subject=original_subject,
        presentation_url=presentation_url(),
        form_url=brief_form_url(),
    )
    if not rendered:
        print(f"send_classified_response: missing template for {category!r}")
        return False

    ok = send_email(email_addr, rendered["subject"], rendered["body"])
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
        # Soft reopen after OOO — helpful email-only pack (no calls).
        ok = send_classified_response(
            "question",
            email_addr,
            item.get("first_name") or "",
            original_subject="following up",
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
    """Persist ambiguous replies for human review and alert ALERT_EMAIL."""
    MANUAL_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    addr = (reply.get("from_email") or "unknown").strip().lower()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^a-z0-9._+-]+", "_", addr)[:80]
    path = MANUAL_REVIEW_DIR / f"{stamp}_{safe}.json"
    payload = {
        "email": addr,
        "to_email": reply.get("to_email", ""),
        "account_email": reply.get("account_email", ""),
        "subject": reply.get("subject", ""),
        "body": reply.get("body") or reply.get("body_preview") or "",
        "received_date": reply.get("received_date", ""),
        "message_id": reply.get("message_id") or reply.get("fingerprint") or "",
        "lead": {
            "contact": (hit or {}).get("contact", ""),
            "company": (hit or {}).get("company", ""),
            "row_index": (hit or {}).get("row_index"),
        },
        "reason": "ambiguous_or_other",
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"manual review queued → {path.name}")
    try:
        from .alerts import send_alert

        body_snip = (reply.get("body") or reply.get("body_preview") or "")[:1500]
        send_alert(
            f"Manual review reply: {addr}",
            (
                f"From: {addr}\nTo/account: {reply.get('account_email')}\n"
                f"Subject: {reply.get('subject')}\n\n{body_snip}\n\nSaved: {path}"
            ),
            severity="info",
        )
    except Exception as e:
        print(f"manual review alert skipped: {e}")
    return path


def _already_sent_response(hit: dict) -> bool:
    blob = f"{hit.get('meeting') or ''} {hit.get('notes') or ''}".lower()
    return "response_sent" in blob or "async_onboarding_sent" in blob


def _match_lead_for_reply(reply: dict, by_email: dict[str, dict]) -> dict | None:
    addr = (reply.get("from_email") or "").strip().lower()
    if addr in by_email:
        return by_email[addr]
    return None


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
    processed_fps: list[str] = []

    seen_sender: set[str] = set()
    for reply in replies:
        if reply.get("is_warmup"):
            fp = reply.get("fingerprint") or reply.get("message_id") or ""
            if fp:
                processed_fps.append(fp)
            skipped.append(reply.get("from_email") or "warmup")
            continue

        addr = (reply.get("from_email") or "").strip().lower()
        fp = reply.get("fingerprint") or reply.get("message_id") or ""
        if not addr:
            continue
        if addr in seen_sender:
            if fp:
                processed_fps.append(fp)
            continue
        seen_sender.add(addr)

        body = reply.get("body") or reply.get("body_preview") or ""
        if reply.get("is_bounce") or _is_bounce_notification_from_text(
            addr, reply.get("subject", ""), body
        ):
            from .bounce_handler import extract_bounced_recipient, record_bounced_email

            recipient = (
                extract_bounced_recipient(addr, reply.get("subject", ""), body) or addr
            )
            record_bounced_email(recipient, source="multi_inbox_replies")
            skipped.append(addr)
            if fp:
                processed_fps.append(fp)
            print(f"bounce detected: {recipient} (via {reply.get('account_email')})")
            continue

        if is_suppressed(addr):
            print(f"skip suppressed: {addr}")
            skipped.append(addr)
            if fp:
                processed_fps.append(fp)
            continue

        hit = _match_lead_for_reply(reply, by_email)
        if not hit:
            classification = classify_reply(body)
            if classification.get("category") == "other":
                forward_to_manual_review(reply, None)
                manual_review.append(addr)
            unmatched.append(addr)
            if fp:
                processed_fps.append(fp)
            continue

        try:
            from .send_throttle import record_reply_for_recipient

            record_reply_for_recipient(addr)
        except Exception:
            pass

        row_index = hit["row_index"]
        classification = classify_reply(body)
        reply_type = classification.get("category") or "other"
        first = (hit.get("contact") or "").split()[0] if hit.get("contact") else ""
        today = datetime.now(timezone.utc).date().isoformat()
        action = ""
        account_tag = reply.get("account_email") or ""

        try:
            update_cell_safe(ws, row_index, REPLIED_COL, "yes")
            update_cell_safe(ws, row_index, REPLY_TYPE_COL, reply_type)

            if reply_type == "unsubscribe":
                add_to_suppression(addr)
                hit["notes"] = _append_note(
                    ws, row_index, hit.get("notes") or "", f"unsubscribed {today}"
                )
                action = "suppressed"

            elif reply_type == "bounce":
                from .bounce_handler import record_bounced_email

                record_bounced_email(addr, source="reply_classify")
                action = "bounced"

            elif reply_type == "out_of_office":
                orig = reply.get("subject") or ""
                if not _already_sent_response(hit):
                    if send_classified_response(
                        "out_of_office",
                        addr,
                        first,
                        original_subject=orig,
                    ):
                        hit["notes"] = _append_note(
                            ws,
                            row_index,
                            hit.get("notes") or "",
                            f"ooo_ack_sent {today}",
                        )
                        responses_sent.append(addr)
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
                action = f"ooo_ack+followup:{follow_date}"

            elif reply_type in ("interested", "question", "not_interested"):
                already = _already_sent_response(hit)
                orig = reply.get("subject") or ""
                if not already:
                    if send_classified_response(
                        reply_type,
                        addr,
                        first,
                        original_subject=orig,
                    ):
                        update_cell_safe(ws, row_index, MEETING_COL, "response_sent")
                        hit["notes"] = _append_note(
                            ws,
                            row_index,
                            hit.get("notes") or "",
                            f"response_sent {reply_type} {today} via:{account_tag}",
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
                orig = reply.get("subject") or ""
                if not _already_sent_response(hit):
                    if send_classified_response(
                        "other",
                        addr,
                        first,
                        original_subject=orig,
                    ):
                        update_cell_safe(ws, row_index, MEETING_COL, "response_sent")
                        hit["notes"] = _append_note(
                            ws,
                            row_index,
                            hit.get("notes") or "",
                            f"response_sent other {today}",
                        )
                        responses_sent.append(addr)
                path = forward_to_manual_review(reply, hit)
                hit["notes"] = _append_note(
                    ws,
                    row_index,
                    hit.get("notes") or "",
                    f"manual_review {today} ({path.name})",
                )
                manual_review.append(addr)
                action = f"other_sent+manual_review:{path.name}"

            else:
                action = "logged"

            if fp:
                processed_fps.append(fp)

            updated.append(
                {
                    "email": addr,
                    "row_index": row_index,
                    "reply_type": reply_type,
                    "language": classification.get("language"),
                    "subject": reply.get("subject", ""),
                    "account_email": account_tag,
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

    _mark_processed(processed_fps)

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
    """Fetch recent replies from all inboxes and sync them into the sheet."""
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
        via = u.get("account_email") or "?"
        print(
            f"  ✓ {u['email']} → type={u['reply_type']} lang={lang} "
            f"via={via} action={u.get('action', '')} (row {u['row_index']})"
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
