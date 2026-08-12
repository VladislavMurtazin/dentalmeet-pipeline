"""Zoho mailbox warmup — internal mail only between SMTP_ACCOUNTS.

Builds sender reputation by sending variable casual emails between our own
accounts and auto-replying / starring / filing them via IMAP.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .email_sender import load_smtp_accounts, send_email

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = ROOT / "data" / "warmup_schedule.json"

IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.zoho.com").strip() or "imap.zoho.com"
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993") or "993")
WARMUP_FOLDER = "Warmup"
WARMUP_HEADER = "X-DentalMeet-Warmup"

# Prefer named pairs when all 5 accounts exist; else sequential among available.
NAMED_PAIR_CHAIN = [
    ("hello", "vlad"),
    ("vlad", "outreach"),
    ("outreach", "founder"),
    ("founder", "vladislav"),
    ("vladislav", "hello"),
]

GREETINGS = ["Hey", "Hi", "Hello", "Yo"]
SIGNOFFS = ["Cheers", "Best", "Talk soon", "Thanks"]
PS_LINES = [
    "P.S. Hope this finds you well",
    "P.S. No rush on this",
    "P.S. Catch you later",
    "P.S. Let me know either way",
]
EMOJIS = ["🎯", "📧"]

CATEGORY_A = [  # Casual check-ins — 40%
    "Hey, how's it going?",
    "Quick question",
    "Thought you'd find this interesting",
    "Following up on our chat",
    "Hope you're having a good week",
]
CATEGORY_B = [  # Industry — 30%
    "Saw this article about dental marketing",
    "Interesting case study",
    "New tool for agencies",
    "Thoughts on this approach?",
]
CATEGORY_C = [  # Personal/business — 30%
    "Weekend plans?",
    "Coffee next week?",
    "Quick favor",
    "Thanks for the help earlier",
]

BODY_BITS_A = [
    "just checking in — things have been busy on my end but mostly good.",
    "wanted to ping you quickly before the day gets away from me.",
    "no big agenda, just keeping the thread warm.",
    "figured I'd drop a short note instead of letting this go cold.",
    "hope the week is treating you alright so far.",
]
BODY_BITS_B = [
    "came across a short write-up on patient acquisition for clinics and it felt relevant.",
    "the case study was light on fluff and heavy on what actually moved bookings.",
    "there's a simple tooling angle here that agencies keep overlooking.",
    "curious how you'd adapt this for a mid-size dental group.",
    "the numbers weren't huge, but the approach was clean and repeatable.",
]
BODY_BITS_C = [
    "if you're free sometime next week I can keep it short — thirty minutes max.",
    "no pressure either way, just tossing the idea out there.",
    "appreciate the assist last time; made a real difference.",
    "weekend's looking quiet for me for once, which feels weirdly nice.",
    "quick favor when you have a sec — nothing urgent.",
]
BODY_FILLERS = [
    "I've been bouncing between a few priorities and trying not to overcomplicate things.",
    "If the timing is off, ignore this and I'll circle back later.",
    "Happy to share more context if useful, otherwise we can leave it here.",
    "Been meaning to send this for a couple days and finally sat down to write it.",
    "Keeping this short on purpose — inbox is crowded enough already.",
]

REPLY_POOL = [
    "Thanks, got it!",
    "Sounds good, will check",
    "Perfect, talk soon",
    "Appreciate it!",
    "Makes sense 👍",
]

DEFAULT_SCHEDULE = {
    "day": 1,
    "emails_sent_today": 0,
    "emails_per_day": 3,
    "last_warmup": None,
    "replies_processed_total": 0,
    "pair_index": 0,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        raw = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def emails_per_day_for(day: int) -> int:
    """Daily warmup volume by program day (day 28+ maintains at 1/day)."""
    d = max(1, int(day))
    if d >= 28:
        return 1
    if d <= 7:
        return 3
    if d <= 14:
        return 5
    if d <= 21:
        return 8
    return 10


def _local_part(addr: str) -> str:
    return (addr or "").split("@", 1)[0].strip().lower()


def _display_name(addr: str) -> str:
    local = _local_part(addr)
    return local.replace(".", " ").replace("_", " ").title() or "there"


def _resolve_alias(accounts: list[tuple[str, str]], alias: str) -> str | None:
    """Match alias to an account local-part (exact only — avoids vlad→vladislav)."""
    alias = (alias or "").strip().lower()
    if not alias:
        return None
    for email_addr, _ in accounts:
        if _local_part(email_addr) == alias:
            return email_addr
    return None


def build_warmup_pairs(accounts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return (sender, recipient) pairs — never external addresses."""
    if len(accounts) < 2:
        return []

    named: list[tuple[str, str]] = []
    for src_alias, dst_alias in NAMED_PAIR_CHAIN:
        src = _resolve_alias(accounts, src_alias)
        dst = _resolve_alias(accounts, dst_alias)
        if src and dst and src.lower() != dst.lower():
            named.append((src, dst))
    if len(named) >= 2:
        return named

    # Fallback: rotate among whatever accounts are configured
    emails = [e for e, _ in accounts]
    return [(emails[i], emails[(i + 1) % len(emails)]) for i in range(len(emails))]


def _account_set(accounts: list[tuple[str, str]]) -> set[str]:
    return {e.lower() for e, _ in accounts}


def _password_for(accounts: list[tuple[str, str]], addr: str) -> str | None:
    target = (addr or "").lower()
    for email_addr, password in accounts:
        if email_addr.lower() == target:
            return password
    return None


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def _expand_to_word_range(parts: list[str], lo: int = 50, hi: int = 100) -> str:
    body = " ".join(p.strip() for p in parts if p and p.strip())
    fillers = list(BODY_FILLERS)
    random.shuffle(fillers)
    i = 0
    while _word_count(body) < lo and i < len(fillers) * 3:
        body = f"{body} {fillers[i % len(fillers)]}"
        i += 1
    words = body.split()
    if len(words) > hi:
        body = " ".join(words[:hi])
    return body


def generate_warmup_content(
    day: int, sender: str, recipient: str
) -> tuple[str, str]:
    """Return (subject, body) with high variability (50–100 words)."""
    roll = random.random()
    if roll < 0.40:
        subject = random.choice(CATEGORY_A)
        bits = BODY_BITS_A
    elif roll < 0.70:
        subject = random.choice(CATEGORY_B)
        bits = BODY_BITS_B
    else:
        subject = random.choice(CATEGORY_C)
        bits = BODY_BITS_C

    greeting = random.choice(GREETINGS)
    signoff = random.choice(SIGNOFFS)
    recip_name = _display_name(recipient)
    sender_name = _display_name(sender)

    openers = [
        f"{greeting} {recip_name},",
        f"{greeting},",
        f"{greeting} {recip_name} —",
    ]
    opener = random.choice(openers)

    core = [
        random.choice(bits),
        random.choice(bits),
        f"We're on day {max(1, int(day))} of the internal warmup loop, "
        "so this is intentionally low-stakes.",
        random.choice(BODY_FILLERS),
    ]
    random.shuffle(core)
    paragraph = _expand_to_word_range(core, 50, 85)

    emoji = ""
    if random.random() < 0.20:
        emoji = f" {random.choice(EMOJIS)}"

    lines = [
        opener,
        "",
        paragraph,
        "",
        f"{signoff},{emoji}".rstrip(","),
        sender_name,
    ]
    if random.random() < 0.45:
        lines.extend(["", random.choice(PS_LINES)])

    body = "\n".join(lines).strip() + "\n"
    # Soft subject spice so messages don't look identical day-to-day
    if random.random() < 0.35:
        spice = random.choice(["", " - quick", " (short)"])
        if spice and spice.strip() not in subject:
            subject = f"{subject}{spice}".strip()
    return subject, body


def load_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        data = dict(DEFAULT_SCHEDULE)
        data["emails_per_day"] = emails_per_day_for(1)
        return data
    try:
        raw = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = dict(DEFAULT_SCHEDULE)
        data["emails_per_day"] = emails_per_day_for(1)
        return data

    data = dict(DEFAULT_SCHEDULE)
    data.update(raw if isinstance(raw, dict) else {})
    data["day"] = max(1, int(data.get("day") or 1))
    data["emails_sent_today"] = max(0, int(data.get("emails_sent_today") or 0))
    data["pair_index"] = max(0, int(data.get("pair_index") or 0))
    data["replies_processed_total"] = max(
        0, int(data.get("replies_processed_total") or 0)
    )
    data["emails_per_day"] = emails_per_day_for(data["day"])
    return data


def save_schedule(data: dict) -> None:
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "day": max(1, int(data.get("day") or 1)),
        "emails_sent_today": max(0, int(data.get("emails_sent_today") or 0)),
        "emails_per_day": emails_per_day_for(int(data.get("day") or 1)),
        "last_warmup": data.get("last_warmup"),
        "replies_processed_total": max(
            0, int(data.get("replies_processed_total") or 0)
        ),
        "pair_index": max(0, int(data.get("pair_index") or 0)),
    }
    SCHEDULE_PATH.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _roll_calendar_day(schedule: dict) -> dict:
    """Advance day / reset daily counter when UTC date changes."""
    now = _utc_now()
    last = _parse_iso(schedule.get("last_warmup"))
    if last is None:
        schedule["emails_per_day"] = emails_per_day_for(schedule["day"])
        return schedule

    if last.date() < now.date():
        days_elapsed = (now.date() - last.date()).days
        schedule["day"] = int(schedule["day"]) + days_elapsed
        schedule["emails_sent_today"] = 0
        schedule["emails_per_day"] = emails_per_day_for(schedule["day"])
        print(
            f"warmup: new UTC day — advanced to day {schedule['day']} "
            f"({schedule['emails_per_day']}/day)"
        )
    else:
        schedule["emails_per_day"] = emails_per_day_for(schedule["day"])
    return schedule


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def send_warmup_emails() -> dict:
    """Send today's remaining warmup emails between internal accounts only."""
    accounts = load_smtp_accounts()
    if len(accounts) < 2:
        print(
            "warmup send error: need at least 2 SMTP_ACCOUNTS "
            "(with matching SMTP_APP_PASSWORDS)"
        )
        return {"sent": 0, "failed": 0, "skipped": True}

    pairs = build_warmup_pairs(accounts)
    if not pairs:
        print("warmup send error: could not build internal pairs")
        return {"sent": 0, "failed": 0, "skipped": True}

    allowed = _account_set(accounts)
    schedule = _roll_calendar_day(load_schedule())
    target = emails_per_day_for(schedule["day"])
    schedule["emails_per_day"] = target
    remaining = max(0, target - int(schedule["emails_sent_today"]))

    print(
        f"warmup send: day {schedule['day']}, "
        f"quota {target}/day, already {schedule['emails_sent_today']}, "
        f"remaining {remaining}, pairs={len(pairs)}"
    )
    if remaining <= 0:
        save_schedule(schedule)
        print("warmup send: daily quota already met")
        return {"sent": 0, "failed": 0, "skipped": False}

    sent = 0
    failed = 0
    pair_index = int(schedule.get("pair_index") or 0)

    for i in range(remaining):
        sender, recipient = pairs[pair_index % len(pairs)]
        pair_index += 1

        if sender.lower() not in allowed or recipient.lower() not in allowed:
            print(
                f"warmup SAFETY skip: refusing non-internal pair "
                f"{sender} -> {recipient}"
            )
            failed += 1
            continue
        if sender.lower() == recipient.lower():
            print(f"warmup skip: same account {sender}")
            continue

        subject, body = generate_warmup_content(schedule["day"], sender, recipient)
        ok = send_email(
            recipient,
            subject,
            body,
            from_email=sender,
            skip_unsubscribe=True,
            extra_headers={WARMUP_HEADER: "1"},
        )
        if ok:
            sent += 1
            schedule["emails_sent_today"] = int(schedule["emails_sent_today"]) + 1
            schedule["last_warmup"] = _iso_now()
            print(f"warmup sent [{sent}/{remaining}]: {sender} -> {recipient}")
        else:
            failed += 1
            print(f"warmup FAILED: {sender} -> {recipient}")

        schedule["pair_index"] = pair_index
        save_schedule(schedule)

        if i < remaining - 1:
            delay = random.randint(120, 300)  # 2–5 minutes
            if _dry_run():
                print(f"(DRY_RUN) would sleep {delay}s before next warmup send")
            else:
                print(f"warmup sleeping {delay}s before next send…")
                time.sleep(delay)

    print(f"warmup send done: sent={sent} failed={failed}")
    return {"sent": sent, "failed": failed, "skipped": False}


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _ensure_folder(mail: imaplib.IMAP4_SSL, name: str) -> str:
    """Create folder if missing; return the mailbox name to use."""
    typ, data = mail.list()
    existing: list[str] = []
    if typ == "OK" and data:
        for item in data:
            if not item:
                continue
            line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
            # LIST reply ends with the mailbox name (possibly quoted)
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                existing.append(m.group(1))

    for candidate in (name, f"INBOX/{name}", f"INBOX.{name}"):
        if candidate in existing:
            return candidate

    for candidate in (name, f"INBOX/{name}"):
        typ, _ = mail.create(candidate)
        if typ == "OK":
            return candidate
        # already exists under another code path
        typ, _ = mail.select(candidate, readonly=True)
        if typ == "OK":
            mail.select("INBOX")
            return candidate
    return name


def _imap_connect(user: str, password: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(user, password)
    return mail


def _process_account_inbox(
    account_email: str,
    password: str,
    peer_emails: set[str],
) -> int:
    """Mark/read/star/reply/move unread warmup mail. Returns replies sent."""
    processed = 0
    mail = None
    try:
        mail = _imap_connect(account_email, password)
        typ, _ = mail.select("INBOX")
        if typ != "OK":
            print(f"warmup IMAP: cannot select INBOX for {account_email}")
            return 0

        folder = _ensure_folder(mail, WARMUP_FOLDER)
        mail.select("INBOX")

        # Unread from any peer — OR search built per peer
        ids: list[bytes] = []
        for peer in sorted(peer_emails):
            if peer == account_email.lower():
                continue
            st, data = mail.search(None, "UNSEEN", "FROM", f'"{peer}"')
            if st == "OK" and data and data[0]:
                ids.extend(data[0].split())

        # de-dupe preserving order
        seen: set[bytes] = set()
        unique_ids: list[bytes] = []
        for num in ids:
            if num not in seen:
                seen.add(num)
                unique_ids.append(num)

        if not unique_ids:
            print(f"warmup IMAP: no unread peer mail in {account_email}")
            return 0

        print(
            f"warmup IMAP: {len(unique_ids)} unread peer message(s) "
            f"in {account_email}"
        )

        for num in unique_ids:
            try:
                st, msg_data = mail.fetch(num, "(RFC822)")
                if st != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email.message_from_bytes(raw)
                from_addr = parseaddr(_decode_header(msg.get("From")))[1].lower()
                if from_addr not in peer_emails or from_addr == account_email.lower():
                    continue

                # Mark read + star (Important)
                mail.store(num, "+FLAGS", "(\\Seen \\Flagged)")

                subject = _decode_header(msg.get("Subject")) or ""
                reply_subject = (
                    subject
                    if subject.lower().startswith("re:")
                    else f"Re: {subject}"
                )
                reply_body = random.choice(REPLY_POOL) + "\n"
                message_id = msg.get("Message-ID") or ""
                extra = {WARMUP_HEADER: "reply"}
                if message_id:
                    extra["In-Reply-To"] = message_id
                    refs = (msg.get("References") or "").strip()
                    extra["References"] = f"{refs} {message_id}".strip()

                if _dry_run():
                    print(
                        f"(DRY_RUN) would reply from {account_email} "
                        f"to {from_addr}: {reply_subject}"
                    )
                    ok = True
                else:
                    ok = send_email(
                        from_addr,
                        reply_subject,
                        reply_body,
                        from_email=account_email,
                        skip_unsubscribe=True,
                        extra_headers=extra,
                    )

                if ok:
                    processed += 1
                else:
                    print(
                        f"warmup reply FAILED {account_email} -> {from_addr}"
                    )

                # Move to Warmup folder
                try:
                    mail.copy(num, folder)
                    mail.store(num, "+FLAGS", "(\\Deleted)")
                except Exception as move_err:
                    print(
                        f"warmup move warn ({account_email}): {move_err}"
                    )

            except Exception as e:
                print(f"warmup process error on msg {num!r} ({account_email}): {e}")

        try:
            mail.expunge()
        except Exception:
            pass

    except Exception as e:
        print(f"warmup IMAP error for {account_email}: {e}")
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    print(f"Processed {processed} warmup replies from {account_email}")
    return processed


def process_warmup_inbox() -> dict:
    """IMAP: read/star/reply/file unread warmup mail across all accounts."""
    accounts = load_smtp_accounts()
    if len(accounts) < 2:
        print("warmup process error: need at least 2 SMTP_ACCOUNTS")
        return {"replies": 0}

    peers = _account_set(accounts)
    total = 0
    for email_addr, password in accounts:
        total += _process_account_inbox(email_addr, password, peers)

    schedule = load_schedule()
    schedule["replies_processed_total"] = (
        int(schedule.get("replies_processed_total") or 0) + total
    )
    save_schedule(schedule)
    print(f"warmup process done: {total} replies total this run")
    return {"replies": total}


def show_stats() -> None:
    schedule = _roll_calendar_day(load_schedule())
    # Don't persist day roll from stats alone unless file missing
    if not SCHEDULE_PATH.exists():
        save_schedule(schedule)
    print("=== warmup stats ===")
    print(f"day: {schedule['day']}")
    print(f"emails_per_day: {emails_per_day_for(schedule['day'])}")
    print(f"emails_sent_today: {schedule['emails_sent_today']}")
    print(f"replies_processed_total: {schedule.get('replies_processed_total', 0)}")
    print(f"last_warmup: {schedule.get('last_warmup') or '(never)'}")
    print(f"pair_index: {schedule.get('pair_index', 0)}")
    accounts = load_smtp_accounts()
    pairs = build_warmup_pairs(accounts)
    print(f"accounts: {len(accounts)}")
    for s, r in pairs:
        print(f"  pair: {s} -> {r}")
    print("=== end stats ===")


def reset_schedule() -> None:
    data = dict(DEFAULT_SCHEDULE)
    data["emails_per_day"] = emails_per_day_for(1)
    data["last_warmup"] = None
    save_schedule(data)
    print("warmup reset: day=1, emails_sent_today=0, replies_processed_total=0")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Zoho internal mailbox warmup")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("send", help="Send today's remaining warmup emails")
    sub.add_parser("process", help="IMAP: reply/star/file unread warmup mail")
    sub.add_parser("stats", help="Show warmup schedule stats")
    sub.add_parser("reset", help="Reset schedule to day 1")

    args = parser.parse_args(argv)
    if args.command == "send":
        send_warmup_emails()
    elif args.command == "process":
        process_warmup_inbox()
    elif args.command == "stats":
        show_stats()
    elif args.command == "reset":
        reset_schedule()


if __name__ == "__main__":
    main()
