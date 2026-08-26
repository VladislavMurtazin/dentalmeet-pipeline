"""Zoho mailbox warmup — internal rotation + optional external recipients.

Each send run: exactly one email FROM each of the 5 accounts.
~70% go to other Zoho mailboxes (internal rotation); ~30% go to
external addresses from data/external_recipients.json.
Process run: IMAP read/star/reply/file peer (internal) mail only —
external replies are handled manually / Gmail auto-responder.
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
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from math import ceil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .email_sender import load_smtp_accounts, send_email

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_PATH = ROOT / "data" / "warmup_schedule.json"
EXTERNAL_PATH = ROOT / "data" / "external_recipients.json"
EXTERNAL_RATIO = 0.30

IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.zoho.com").strip() or "imap.zoho.com"
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993") or "993")
WARMUP_FOLDER = "Warmup"
WARMUP_HEADER = "X-DentalMeet-Warmup"

# Canonical order for the 5 warmup mailboxes
ACCOUNT_ORDER = ("hello", "vlad", "outreach", "founder", "vladislav")
REQUIRED_ACCOUNTS = 5

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

# External (Gmail etc.) — shorter / more casual
EXTERNAL_SUBJECTS = [
    "quick thought",
    "saw this",
    "thought of you",
    "question for you",
    "random thought",
    "one sec",
    "curious",
    "hey - quick one",
]
EXTERNAL_TOPICS = [
    "patient recall emails",
    "Google reviews for clinics",
    "short-form dental video",
    "referral loops",
    "booking-page friction",
    "local SEO for practices",
    "weekend openers",
    "simple CRM follow-ups",
]
EXTERNAL_BODIES = [
    "Quick one - what do you think about {topic}? I've been chewing on it "
    "for a day and would love a gut check when you have a sec. No rush.",
    "Thought you'd find this interesting: {topic}. It felt practical, not "
    "fluffy, and I wondered if it lines up with what you're seeing lately.",
    "Random thought on {topic} - does that match your world right now? "
    "Curious either way, even a one-liner helps.",
    "Quick ping: ever tried anything around {topic}? Not pitching anything, "
    "just collecting notes from people I trust.",
    "Saw something on {topic} and immediately thought of you. If it's noise, "
    "ignore me - otherwise curious what you'd tweak first.",
    "No agenda - just wondering your take on {topic}. Happy to trade notes "
    "later if useful; otherwise all good.",
]
EXTERNAL_FILLERS = [
    "Keeping this short on purpose.",
    "Totally fine if the timing is off.",
    "Can share the source if you want it.",
]
EXTERNAL_PS = [
    "P.S. Totally fine if this is a later thing.",
    "P.S. Ignore if you're slammed.",
    "P.S. Coffee chat someday would be fun.",
    "P.S. No reply needed unless you want to.",
    "P.S. Hope the week's treating you well.",
]

WARMUP_PERIOD_DAYS = 28  # peak ends; cold outreach typically starts after
RAMP_DOWN_END_DAY = 40
SEND_INTERVAL_HOURS = 4  # matches CI cron "0 */4 * * *"

DEFAULT_SCHEDULE = {
    "day": 1,
    "runs_today": 0,
    "emails_sent_today": 0,
    "total_sent": 0,
    "total_replies": 0,
    "internal_sent": 0,
    "external_sent": 0,
    "last_send": None,
    "last_process": None,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def max_runs_per_day(day: int) -> int:
    """Base daily send-run cap (before real-outreach reduction).

    Days 1-7: 3, 8-14: 4, 15-21: 5, 22-28: 6 (peak),
    29-32: 4, 33-36: 3, 37-40: 2, 41+: 1 (maintenance).
    """
    d = max(1, int(day))
    if d <= 7:
        return 3
    if d <= 14:
        return 4
    if d <= 21:
        return 5
    if d <= 28:
        return 6
    if d <= 32:
        return 4
    if d <= 36:
        return 3
    if d <= 40:
        return 2
    return 1


def warmup_phase_label(day: int) -> str:
    d = max(1, int(day))
    if d <= 21:
        return "ramp-up phase"
    if d <= 28:
        return "peak phase"
    if d <= RAMP_DOWN_END_DAY:
        return "ramp-down phase"
    return "maintenance phase"


def effective_runs_per_day(day: int, *, real_sends_today: int = 0) -> tuple[int, int, bool]:
    """Return (effective_cap, base_cap, reduced_due_to_real_sends).

    If the real cold-send job already sent >0 emails today, cut warmup
    runs by 50% (floor, minimum 1) so accounts aren't overloaded.
    """
    base = max_runs_per_day(day)
    if int(real_sends_today or 0) > 0:
        reduced = max(1, base // 2)
        return reduced, base, True
    return base, base, False


def count_real_sends_today(*, today: date | None = None) -> int:
    """Count Agencies rows whose Sent column is today's date (YYYY-MM-DD)."""
    today_str = (today or _utc_now().date()).isoformat()
    try:
        from .to_sheets import HEADERS, get_all_values_safe, get_worksheet_safe

        ws = get_worksheet_safe()
        values = get_all_values_safe(ws)
        if not values:
            return 0
        header = [(h or "").strip() for h in values[0]]
        try:
            i_sent = header.index("Sent")
        except ValueError:
            try:
                i_sent = HEADERS.index("Sent")
            except ValueError:
                return 0
        n = 0
        for row in values[1:]:
            if i_sent >= len(row):
                continue
            cell = (row[i_sent] or "").strip()
            if not cell:
                continue
            if cell == today_str or cell.startswith(today_str):
                n += 1
                continue
            raw = cell.replace("Z", "").split("T", 1)[0]
            if raw == today_str:
                n += 1
        return n
    except Exception as e:
        print(f"warmup: could not read real sends today ({e})")
        return 0


def _local_part(addr: str) -> str:
    return (addr or "").split("@", 1)[0].strip().lower()


def _display_name(addr: str) -> str:
    local = _local_part(addr)
    return local.replace(".", " ").replace("_", " ").title() or "there"


def _resolve_alias(accounts: list[tuple[str, str]], alias: str) -> str | None:
    """Exact local-part match only (avoids vlad → vladislav)."""
    alias = (alias or "").strip().lower()
    if not alias:
        return None
    for email_addr, _ in accounts:
        if _local_part(email_addr) == alias:
            return email_addr
    return None


def ordered_warmup_accounts(
    accounts: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Return the 5 accounts in canonical order, or [] if incomplete."""
    accounts = accounts if accounts is not None else load_smtp_accounts()
    by_email = {email.lower(): (email, pwd) for email, pwd in accounts}
    ordered: list[tuple[str, str]] = []
    for alias in ACCOUNT_ORDER:
        resolved = _resolve_alias(accounts, alias)
        if not resolved:
            return []
        ordered.append(by_email[resolved.lower()])
    if len(ordered) != REQUIRED_ACCOUNTS:
        return []
    # Safety: all distinct
    if len({e.lower() for e, _ in ordered}) != REQUIRED_ACCOUNTS:
        return []
    return ordered


def build_run_pairs(
    emails: list[str], *, offset: int
) -> list[tuple[str, str]]:
    """One FROM each account; recipient = (i + offset) % n (offset 1..n-1)."""
    n = len(emails)
    if n < 2:
        return []
    shift = ((int(offset) - 1) % (n - 1)) + 1  # keep in 1..n-1
    pairs: list[tuple[str, str]] = []
    for i, sender in enumerate(emails):
        recipient = emails[(i + shift) % n]
        if sender.lower() == recipient.lower():
            continue
        pairs.append((sender, recipient))
    return pairs


def pair_offset_for_total_sent(total_sent: int) -> int:
    """Shift recipient index: run 1→+1, run 2→+2, … cycles every 4 runs."""
    runs_done = max(0, int(total_sent)) // REQUIRED_ACCOUNTS
    return (runs_done % (REQUIRED_ACCOUNTS - 1)) + 1


def _account_set(accounts: list[tuple[str, str]]) -> set[str]:
    return {e.lower() for e, _ in accounts}


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
    """Return (subject, body) with high variability (50–100 words). Internal only."""
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
    if random.random() < 0.35:
        spice = random.choice(["", " - quick", " (short)"])
        if spice and spice.strip() not in subject:
            subject = f"{subject}{spice}".strip()
    return subject, body


def generate_external_warmup_content(
    sender: str, recipient: str, *, recipient_name: str = ""
) -> tuple[str, str]:
    """Casual short content for external inboxes (≈30–50 words)."""
    subject = random.choice(EXTERNAL_SUBJECTS)
    topic = random.choice(EXTERNAL_TOPICS)
    name = (recipient_name or "").strip() or _display_name(recipient)
    sender_name = _display_name(sender)
    greeting = random.choice(["Hey", "Hi", "Hey there"])

    core = random.choice(EXTERNAL_BODIES).format(topic=topic)
    if random.random() < 0.55:
        core = f"{core} {random.choice(EXTERNAL_FILLERS)}"

    if random.random() < 0.65:
        opener = f"{greeting} {name},"
    else:
        opener = f"{greeting},"

    lines = [opener, "", core]
    # P.S. more often than internal (~70%)
    if random.random() < 0.70:
        lines.extend(["", random.choice(EXTERNAL_PS)])
    lines.extend(["", random.choice(["Cheers", "Talk soon", "Later"]), sender_name])

    body = "\n".join(lines).strip() + "\n"
    # Nudge into 30-50 word band
    fillers = list(EXTERNAL_FILLERS)
    random.shuffle(fillers)
    fi = 0
    while _word_count(body) < 30 and fi < len(fillers):
        body = body.rstrip() + f"\n\n{fillers[fi]}\n"
        fi += 1
    words = body.split()
    if len(words) > 50:
        body = " ".join(words[:50]) + "\n"
    return subject, body


def load_external_recipients() -> list[dict]:
    """Load [{email, name}, ...] from data/external_recipients.json."""
    if not EXTERNAL_PATH.exists():
        return []
    try:
        raw = json.loads(EXTERNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("external") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        email_addr = str(item.get("email") or "").strip()
        if not email_addr or "@" not in email_addr:
            continue
        key = email_addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "email": email_addr,
                "name": str(item.get("name") or "").strip() or _display_name(email_addr),
            }
        )
    return out


def save_external_recipients(recipients: list[dict]) -> None:
    EXTERNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "external": [
            {
                "email": r["email"],
                "name": r.get("name") or _display_name(r["email"]),
            }
            for r in recipients
        ]
    }
    EXTERNAL_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def add_external_recipient(email_addr: str, name: str = "") -> dict:
    """Add or update an external recipient. Returns the saved record."""
    email_addr = (email_addr or "").strip()
    if not email_addr or "@" not in email_addr:
        raise ValueError(f"invalid email: {email_addr!r}")
    name = (name or "").strip() or _display_name(email_addr)
    recipients = load_external_recipients()
    key = email_addr.lower()
    for rec in recipients:
        if rec["email"].lower() == key:
            rec["email"] = email_addr
            rec["name"] = name
            save_external_recipients(recipients)
            print(f"Updated external recipient: {email_addr} ({name})")
            return rec
    rec = {"email": email_addr, "name": name}
    recipients.append(rec)
    save_external_recipients(recipients)
    print(f"Added external recipient: {email_addr} ({name})")
    return rec


def mix_internal_external_pairs(
    internal_pairs: list[tuple[str, str]],
    external: list[dict],
    *,
    ratio: float = EXTERNAL_RATIO,
) -> list[tuple[str, str, str, str]]:
    """Return (sender, recipient, kind, recip_name) with ~ratio external.

    kind is ``internal`` or ``external``. Internal rotation pairs are kept
    for non-external slots; external slots pick a random external address.
    """
    n = len(internal_pairs)
    if n == 0:
        return []
    n_ext = 0
    if external:
        n_ext = int(round(n * float(ratio)))
        n_ext = max(0, min(n, n_ext))
        # Prefer at least 1 external when list is non-empty and n>=3
        if n >= 3 and n_ext == 0 and ratio > 0:
            n_ext = 1

    slots = list(range(n))
    random.shuffle(slots)
    ext_slots = set(slots[:n_ext])

    mixed: list[tuple[str, str, str, str]] = []
    for i, (sender, recip) in enumerate(internal_pairs):
        if i in ext_slots:
            choice = random.choice(external)
            # Avoid accidental self if external somehow matches a Zoho alias
            tries = 0
            while (
                choice["email"].lower() == sender.lower()
                and len(external) > 1
                and tries < 5
            ):
                choice = random.choice(external)
                tries += 1
            if choice["email"].lower() == sender.lower():
                mixed.append((sender, recip, "internal", ""))
            else:
                mixed.append(
                    (
                        sender,
                        choice["email"],
                        "external",
                        choice.get("name") or "",
                    )
                )
        else:
            mixed.append((sender, recip, "internal", ""))
    return mixed


def _normalize_schedule(raw: dict | None = None) -> dict:
    data = dict(DEFAULT_SCHEDULE)
    if isinstance(raw, dict):
        # Migrate older key names
        migrated = dict(raw)
        if "total_sent" not in migrated and "total_emails_sent" in migrated:
            migrated["total_sent"] = migrated.get("total_emails_sent")
        if "total_replies" not in migrated and "replies_processed_total" in migrated:
            migrated["total_replies"] = migrated.get("replies_processed_total")
        if "last_send" not in migrated and migrated.get("last_warmup"):
            migrated["last_send"] = migrated.get("last_warmup")
        data.update(migrated)

    data["day"] = max(1, int(data.get("day") or 1))
    data["runs_today"] = max(0, int(data.get("runs_today") or 0))
    data["emails_sent_today"] = max(0, int(data.get("emails_sent_today") or 0))
    data["total_sent"] = max(0, int(data.get("total_sent") or 0))
    data["total_replies"] = max(0, int(data.get("total_replies") or 0))
    data["internal_sent"] = max(0, int(data.get("internal_sent") or 0))
    data["external_sent"] = max(0, int(data.get("external_sent") or 0))
    data["last_send"] = data.get("last_send") or None
    data["last_process"] = data.get("last_process") or None
    return data


def load_schedule(*, create_if_missing: bool = False) -> dict:
    """Load warmup_schedule.json; optionally create with defaults if missing."""
    if not SCHEDULE_PATH.exists():
        data = _normalize_schedule()
        if create_if_missing:
            save_schedule(data)
        return data
    try:
        raw = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = _normalize_schedule()
        if create_if_missing:
            save_schedule(data)
        return data
    return _normalize_schedule(raw if isinstance(raw, dict) else None)


def save_schedule(data: dict) -> None:
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "day": max(1, int(data.get("day") or 1)),
        "runs_today": max(0, int(data.get("runs_today") or 0)),
        "emails_sent_today": max(0, int(data.get("emails_sent_today") or 0)),
        "total_sent": max(0, int(data.get("total_sent") or 0)),
        "total_replies": max(0, int(data.get("total_replies") or 0)),
        "internal_sent": max(0, int(data.get("internal_sent") or 0)),
        "external_sent": max(0, int(data.get("external_sent") or 0)),
        "last_send": data.get("last_send"),
        "last_process": data.get("last_process"),
    }
    SCHEDULE_PATH.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _last_activity(schedule: dict) -> datetime | None:
    a = _parse_iso(schedule.get("last_send"))
    b = _parse_iso(schedule.get("last_process"))
    if a and b:
        return max(a, b)
    return a or b


def _roll_calendar_day(schedule: dict) -> dict:
    """Advance day / reset daily counters when the UTC date changes."""
    now = _utc_now()
    last = _last_activity(schedule)
    if last is None:
        return schedule

    if last.date() < now.date():
        days_elapsed = (now.date() - last.date()).days
        schedule["day"] = int(schedule["day"]) + days_elapsed
        schedule["runs_today"] = 0
        schedule["emails_sent_today"] = 0
        d = int(schedule["day"])
        print(
            f"warmup: new UTC day — advanced to day {d} "
            f"({warmup_phase_label(d)}, "
            f"base {max_runs_per_day(d)} runs/day)"
        )
    return schedule


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _sleep(seconds: int, *, label: str) -> None:
    if _dry_run():
        print(f"(DRY_RUN) would sleep {seconds}s {label}")
        return
    print(f"warmup sleeping {seconds}s {label}…")
    time.sleep(seconds)


def send_warmup_emails() -> dict:
    """One send run: 5 emails (one FROM each), ~70% internal / ~30% external."""
    accounts = ordered_warmup_accounts()
    if len(accounts) != REQUIRED_ACCOUNTS:
        print(
            "warmup send error: need exactly 5 SMTP_ACCOUNTS matching "
            f"{', '.join(ACCOUNT_ORDER)} (with SMTP_APP_PASSWORDS)"
        )
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "internal_sent": 0,
            "external_sent": 0,
        }

    emails = [e for e, _ in accounts]
    allowed_senders = set(a.lower() for a in emails)
    external = load_external_recipients()

    schedule = _roll_calendar_day(load_schedule(create_if_missing=True))
    day = int(schedule["day"])
    real_sends = count_real_sends_today()
    run_cap, base_cap, reduced = effective_runs_per_day(
        day, real_sends_today=real_sends
    )
    runs_today = int(schedule["runs_today"])
    phase = warmup_phase_label(day)

    print(
        f"Warmup day: {day} ({phase}), Real sends today: {real_sends}, "
        f"Warmup runs today: {runs_today}/{run_cap}"
        + (" (reduced due to real sends)" if reduced else "")
    )
    print(
        f"warmup send: base_cap={base_cap}, effective_cap={run_cap}, "
        f"warmup_emails_today={schedule['emails_sent_today']}, "
        f"total_sent={schedule['total_sent']}, "
        f"external_recipients={len(external)}"
    )

    if runs_today >= run_cap:
        save_schedule(schedule)
        print("Daily warmup limit reached, skipping")
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "internal_sent": 0,
            "external_sent": 0,
        }

    offset = pair_offset_for_total_sent(int(schedule["total_sent"]))
    internal_pairs = build_run_pairs(emails, offset=offset)
    if len(internal_pairs) != REQUIRED_ACCOUNTS:
        print(
            f"warmup send error: expected {REQUIRED_ACCOUNTS} pairs, "
            f"got {len(internal_pairs)}"
        )
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "internal_sent": 0,
            "external_sent": 0,
        }

    mixed = mix_internal_external_pairs(
        internal_pairs, external, ratio=EXTERNAL_RATIO
    )
    n_ext_planned = sum(1 for _s, _r, kind, _n in mixed if kind == "external")
    n_int_planned = len(mixed) - n_ext_planned

    print(
        f"warmup send: pair offset +{offset} "
        f"(planned internal={n_int_planned} external={n_ext_planned})"
    )
    for sender, recipient, kind, _name in mixed:
        tag = "EXTERNAL" if kind == "external" else "internal"
        print(f"  planned [{tag}]: {sender} -> {recipient}")

    sent = 0
    failed = 0
    internal_sent = 0
    external_sent = 0

    for i, (sender, recipient, kind, recip_name) in enumerate(mixed):
        if sender.lower() not in allowed_senders:
            print(f"warmup SAFETY skip: unknown sender {sender}")
            failed += 1
            continue
        if kind == "internal" and recipient.lower() not in allowed_senders:
            print(
                f"warmup SAFETY skip: refusing non-internal pair "
                f"{sender} -> {recipient}"
            )
            failed += 1
            continue
        if sender.lower() == recipient.lower():
            print(f"warmup SAFETY skip: same account {sender}")
            failed += 1
            continue

        if kind == "external":
            subject, body = generate_external_warmup_content(
                sender, recipient, recipient_name=recip_name
            )
            header_val = "external"
        else:
            subject, body = generate_warmup_content(day, sender, recipient)
            header_val = "1"

        ok = send_email(
            recipient,
            subject,
            body,
            from_email=sender,
            skip_unsubscribe=True,
            extra_headers={WARMUP_HEADER: header_val},
        )
        if ok:
            sent += 1
            schedule["emails_sent_today"] = int(schedule["emails_sent_today"]) + 1
            schedule["total_sent"] = int(schedule["total_sent"]) + 1
            if kind == "external":
                external_sent += 1
                schedule["external_sent"] = int(schedule.get("external_sent") or 0) + 1
            else:
                internal_sent += 1
                schedule["internal_sent"] = int(schedule.get("internal_sent") or 0) + 1
            schedule["last_send"] = _iso_now()
            tag = "EXTERNAL" if kind == "external" else "internal"
            print(
                f"warmup sent [{sent}/5] [{tag}]: {sender} -> {recipient} "
                f"| subject={subject!r}"
            )
        else:
            failed += 1
            print(f"warmup FAILED: {sender} -> {recipient}")

        save_schedule(schedule)

        if i < len(mixed) - 1:
            _sleep(random.randint(30, 90), label="before next warmup send")

    # Count as a run if we attempted the full batch (even with some failures)
    schedule["runs_today"] = int(schedule["runs_today"]) + 1
    schedule["last_send"] = schedule.get("last_send") or _iso_now()
    save_schedule(schedule)

    print(
        f"warmup send done: sent={sent} failed={failed} "
        f"internal={internal_sent} external={external_sent} "
        f"runs_today={schedule['runs_today']}/{run_cap}"
    )
    print(
        f"Internal sent: {schedule.get('internal_sent', 0)}, "
        f"External sent: {schedule.get('external_sent', 0)}"
    )
    return {
        "sent": sent,
        "failed": failed,
        "skipped": False,
        "internal_sent": internal_sent,
        "external_sent": external_sent,
    }


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
            line = (
                item.decode("utf-8", errors="replace")
                if isinstance(item, bytes)
                else str(item)
            )
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
        typ, _ = mail.select(candidate, readonly=True)
        if typ == "OK":
            mail.select("INBOX")
            return candidate
    return name


def _imap_connect(user: str, password: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(user, password)
    return mail


def _collect_unread_peer_ids(
    mail: imaplib.IMAP4_SSL,
    account_email: str,
    peer_emails: set[str],
) -> list[bytes]:
    ids: list[bytes] = []
    for peer in sorted(peer_emails):
        if peer == account_email.lower():
            continue
        st, data = mail.search(None, "UNSEEN", "FROM", f'"{peer}"')
        if st == "OK" and data and data[0]:
            ids.extend(data[0].split())

    seen: set[bytes] = set()
    unique: list[bytes] = []
    for num in ids:
        if num not in seen:
            seen.add(num)
            unique.append(num)
    return unique


def _process_account_inbox(
    account_email: str,
    password: str,
    peer_emails: set[str],
    *,
    delay_between: bool = True,
) -> int:
    """Mark/read/star/reply/move unread peer mail. Returns replies sent."""
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
        unique_ids = _collect_unread_peer_ids(mail, account_email, peer_emails)

        if not unique_ids:
            print(f"warmup IMAP: no unread peer mail in {account_email}")
            return 0

        print(
            f"warmup IMAP: {len(unique_ids)} unread peer message(s) "
            f"in {account_email}"
        )

        for idx, num in enumerate(unique_ids):
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
                    print(
                        f"warmup replied [{processed}]: "
                        f"{account_email} -> {from_addr}"
                    )
                else:
                    print(f"warmup reply FAILED {account_email} -> {from_addr}")

                try:
                    mail.copy(num, folder)
                    mail.store(num, "+FLAGS", "(\\Deleted)")
                except Exception as move_err:
                    print(f"warmup move warn ({account_email}): {move_err}")

                if delay_between and idx < len(unique_ids) - 1:
                    _sleep(
                        random.randint(10, 30),
                        label="before next warmup reply",
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
    """IMAP: read/star/reply/file unread peer mail across all 5 accounts."""
    accounts = ordered_warmup_accounts()
    if len(accounts) != REQUIRED_ACCOUNTS:
        print(
            "warmup process error: need exactly 5 SMTP_ACCOUNTS matching "
            f"{', '.join(ACCOUNT_ORDER)}"
        )
        return {"replies": 0}

    peers = _account_set(accounts)
    total = 0
    for email_addr, password in accounts:
        total += _process_account_inbox(email_addr, password, peers)

    schedule = _roll_calendar_day(load_schedule(create_if_missing=True))
    schedule["total_replies"] = int(schedule.get("total_replies") or 0) + total
    schedule["last_process"] = _iso_now()
    save_schedule(schedule)
    print(f"warmup process done: {total} replies total this run")
    return {"replies": total}


def _format_ts(value: str | None) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "never"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _next_warmup_label(last_send: str | None, now: datetime | None = None) -> str:
    now = now or _utc_now()
    last = _parse_iso(last_send)
    if last is None:
        return "now"
    next_at = last + timedelta(hours=SEND_INTERVAL_HOURS)
    seconds = (next_at - now).total_seconds()
    if seconds <= 0:
        return "now"
    hours = max(1, int(ceil(seconds / 3600)))
    if hours == 1:
        return "in 1 hour"
    return f"in {hours} hours"


def _status_label(day: int) -> str:
    phase = warmup_phase_label(day)
    base = max_runs_per_day(day)
    return f"{phase.upper()} ({base} run(s)/day base)"


def show_stats() -> None:
    """Print human-readable warmup stats; create schedule file if missing."""
    schedule = _roll_calendar_day(load_schedule(create_if_missing=True))
    save_schedule(schedule)
    external = load_external_recipients()

    day = int(schedule["day"])
    real_sends = count_real_sends_today()
    run_cap, base_cap, reduced = effective_runs_per_day(
        day, real_sends_today=real_sends
    )
    phase = warmup_phase_label(day)
    runs_today = int(schedule["runs_today"])

    print("=== WARMUP STATS ===")
    print(
        f"Warmup day: {day} ({phase}), Real sends today: {real_sends}, "
        f"Warmup runs today: {runs_today}/{run_cap}"
        + (" (reduced due to real sends)" if reduced else "")
    )
    print(f"Base runs/day: {base_cap} (effective: {run_cap})")
    print(f"Warmup emails sent today: {schedule['emails_sent_today']}")
    print(f"Total warmup emails sent: {schedule['total_sent']}")
    print(
        f"Internal sent: {schedule.get('internal_sent', 0)}, "
        f"External sent: {schedule.get('external_sent', 0)}"
    )
    print(f"Total replies processed: {schedule['total_replies']}")
    print(f"External recipients configured: {len(external)}")
    for rec in external:
        print(f"  - {rec['email']} ({rec.get('name') or ''})")
    print(f"Last send: {_format_ts(schedule.get('last_send'))}")
    print(f"Last process: {_format_ts(schedule.get('last_process'))}")
    print(f"Next warmup: {_next_warmup_label(schedule.get('last_send'))}")
    print(f"Status: {_status_label(day)}")
    print("====================")


def reset_schedule() -> None:
    data = _normalize_schedule()
    save_schedule(data)
    print(
        "warmup reset: day=1, runs_today=0, emails_sent_today=0, "
        "total_sent=0, total_replies=0, internal_sent=0, external_sent=0"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Zoho mailbox warmup (internal + external recipients)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "send",
        help="Send one warmup run (5 emails; ~70%% internal / ~30%% external)",
    )
    sub.add_parser("process", help="IMAP: reply/star/file unread peer mail")
    sub.add_parser("stats", help="Show warmup schedule stats")
    sub.add_parser("reset", help="Reset schedule to day 1")

    p_add = sub.add_parser(
        "add-external",
        help="Add an external warmup recipient to data/external_recipients.json",
    )
    p_add.add_argument("--email", required=True, help="External email address")
    p_add.add_argument("--name", default="", help="Display name")

    args = parser.parse_args(argv)
    if args.command == "send":
        send_warmup_emails()
    elif args.command == "process":
        process_warmup_inbox()
    elif args.command == "stats":
        show_stats()
    elif args.command == "reset":
        reset_schedule()
    elif args.command == "add-external":
        add_external_recipient(args.email, args.name)


if __name__ == "__main__":
    main()
