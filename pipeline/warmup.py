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
# 0 = internal-only unless passive externals exist (see effective_external_ratio).
EXTERNAL_RATIO = float(os.environ.get("WARMUP_EXTERNAL_RATIO", "0") or "0")
# Receiver-only Gmail seeds — NEVER send FROM these; never IMAP/SMTP; not in accounts.json.
WARMUP_EXTERNAL_PASSIVE = [
    "trestovtrest8@gmail.com",
]

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
    "matrix_events": [],
    "account_stats": {},
    "last_pairs": {},
    "processed_threads": [],
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
    """
    Return warmup accounts: canonical Zoho 5 (if present) then Google backups.
    Requires at least the 5 Zoho aliases when Zoho passwords are configured;
    otherwise returns all warmup-enabled accounts with passwords.
    """
    from .smtp_accounts import load_warmup_smtp_accounts

    if accounts is None:
        accounts = load_warmup_smtp_accounts(
            allow_dry_run_placeholders=_dry_run()
        )
    by_email = {email.lower(): (email, pwd) for email, pwd in accounts}

    ordered: list[tuple[str, str]] = []
    for alias in ACCOUNT_ORDER:
        resolved = _resolve_alias(accounts, alias)
        if resolved:
            ordered.append(by_email[resolved.lower()])

    # Append remaining warmup accounts (Google meet-agency.com, etc.)
    seen = {e.lower() for e, _ in ordered}
    for email, pwd in accounts:
        if email.lower() not in seen:
            ordered.append((email, pwd))
            seen.add(email.lower())

    if len(ordered) < 2:
        return []
    return ordered


def warmup_account_count(accounts: list[tuple[str, str]] | None = None) -> int:
    return len(ordered_warmup_accounts(accounts))


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


def provider_of(email_addr: str) -> str:
    """zoho | google | external — for cross-provider preference."""
    key = (email_addr or "").strip().lower()
    if is_passive_external(key):
        return "external"
    try:
        from .smtp_accounts import get_account

        acc = get_account(key)
        if acc and acc.get("provider"):
            return str(acc["provider"]).lower()
    except Exception:
        pass
    domain = key.split("@")[-1] if "@" in key else ""
    if "meet-agency" in domain or domain == "gmail.com":
        return "google"
    if "dentalmeet" in domain:
        return "zoho"
    return "external"


def pick_recipient(
    sender: str,
    all_recipients: list[str],
    *,
    last_pair: tuple[str, str] | None = None,
    cross_prob: float = 0.70,
) -> str | None:
    """Pick recipient ≠ sender; ~70% prefer cross-provider; avoid last pair."""
    sender_l = (sender or "").strip().lower()
    candidates = [
        r for r in all_recipients if (r or "").strip().lower() != sender_l
    ]
    if not candidates:
        return None

    if last_pair:
        last_s, last_r = last_pair[0].lower(), last_pair[1].lower()
        if last_s == sender_l:
            filtered = [r for r in candidates if r.lower() != last_r]
            if filtered:
                candidates = filtered

    sender_provider = provider_of(sender)
    cross = [r for r in candidates if provider_of(r) != sender_provider]
    if cross and random.random() < float(cross_prob):
        return random.choice(cross)
    return random.choice(candidates)


def build_cross_warmup_pairs(
    senders: list[str],
    *,
    last_pairs: dict[str, str] | None = None,
    include_external: bool = True,
) -> list[tuple[str, str, str, str]]:
    """
    One send FROM each sender; recipient from other senders + passive/file externals.
    Returns (sender, recipient, kind, recip_name) with kind internal|external.
    """
    last_pairs = last_pairs or {}
    recipient_pool = list(senders)
    external_recs = load_external_recipients() if include_external else []
    ext_by_email = {r["email"].lower(): r for r in external_recs}
    for rec in external_recs:
        e = rec["email"]
        if e.lower() not in {x.lower() for x in recipient_pool}:
            recipient_pool.append(e)

    mixed: list[tuple[str, str, str, str]] = []
    used_recipients: set[str] = set()
    for sender in senders:
        # Soft balance: prefer recipients not already targeted this run
        prefer = [
            r
            for r in recipient_pool
            if r.lower() != sender.lower() and r.lower() not in used_recipients
        ]
        pool = prefer if prefer else recipient_pool
        last = last_pairs.get(sender.lower())
        last_pair = (sender, last) if last else None
        recip = pick_recipient(sender, pool, last_pair=last_pair)
        if not recip:
            continue
        used_recipients.add(recip.lower())
        if recip.lower() in ext_by_email or is_passive_external(recip):
            name = (ext_by_email.get(recip.lower()) or {}).get("name") or ""
            mixed.append((sender, recip, "external", name))
        else:
            mixed.append((sender, recip, "internal", ""))
    return mixed


def pair_offset_for_total_sent(total_sent: int, n_accounts: int | None = None) -> int:
    """Shift recipient index: run 1→+1, run 2→+2, … cycles every (n-1) runs."""
    n = int(n_accounts or REQUIRED_ACCOUNTS)
    if n < 2:
        return 1
    runs_done = max(0, int(total_sent)) // n
    return (runs_done % (n - 1)) + 1


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


def _week_start_utc(d: date | None = None) -> str:
    d = d or datetime.now(timezone.utc).date()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def effective_external_ratio() -> float:
    """Env ratio; if 0 but passive externals exist, use a small default so they get mail."""
    ratio = float(EXTERNAL_RATIO)
    if ratio > 0:
        return ratio
    if WARMUP_EXTERNAL_PASSIVE:
        return 0.20
    return 0.0


def passive_external_emails() -> list[str]:
    """Normalized passive receiver-only addresses (hardcoded + never senders)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in WARMUP_EXTERNAL_PASSIVE:
        email_addr = (raw or "").strip().lower()
        if not email_addr or "@" not in email_addr or email_addr in seen:
            continue
        seen.add(email_addr)
        out.append(email_addr)
    return out


def is_passive_external(email_addr: str) -> bool:
    return (email_addr or "").strip().lower() in set(passive_external_emails())


def load_passive_external_recipients() -> list[dict]:
    return [
        {
            "email": e,
            "name": _display_name(e),
            "role": "passive",
        }
        for e in passive_external_emails()
    ]


def load_external_recipients() -> list[dict]:
    """Load file externals + merge hardcoded passive receivers."""
    if not EXTERNAL_PATH.exists():
        file_items: list[dict] = []
    else:
        try:
            raw = json.loads(EXTERNAL_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            file_items = []
        else:
            items = raw.get("external") if isinstance(raw, dict) else None
            file_items = items if isinstance(items, list) else []

    out: list[dict] = []
    seen: set[str] = set()
    for item in file_items:
        if not isinstance(item, dict):
            continue
        email_addr = str(item.get("email") or "").strip()
        if not email_addr or "@" not in email_addr:
            continue
        key = email_addr.lower()
        if key in seen:
            continue
        # Never promote a passive address into an active/file identity conflict
        role = "passive" if key in set(passive_external_emails()) else "external"
        seen.add(key)
        out.append(
            {
                "email": email_addr,
                "name": str(item.get("name") or "").strip() or _display_name(email_addr),
                "role": role,
            }
        )
    for rec in load_passive_external_recipients():
        key = rec["email"].lower()
        if key in seen:
            # Ensure role stays passive
            for existing in out:
                if existing["email"].lower() == key:
                    existing["role"] = "passive"
            continue
        seen.add(key)
        out.append(rec)
    return out


def list_external_recipients() -> None:
    """CLI: print passive + file external warmup recipients."""
    print("=== WARMUP EXTERNAL ===")
    passive = load_passive_external_recipients()
    print(f"Passive (receiver-only, hardcoded): {len(passive)}")
    for rec in passive:
        print(f"  - {rec['email']}  [passive — never sender / never IMAP]")
    file_only = [
        r
        for r in load_external_recipients()
        if r.get("role") != "passive"
    ]
    print(f"File list (data/external_recipients.json): {len(file_only)}")
    for rec in file_only:
        print(f"  - {rec['email']} ({rec.get('name') or ''})")
    print(f"Effective external ratio: {effective_external_ratio():.0%}")
    print("=======================")


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
    if is_passive_external(email_addr):
        raise ValueError(
            f"{email_addr} is a hardcoded PASSIVE external (receiver-only). "
            "Do not add it to external_recipients.json or accounts.json."
        )
    name = (name or "").strip() or _display_name(email_addr)
    # File-only list (exclude passive hardcoded)
    file_recs = [
        r for r in load_external_recipients() if r.get("role") != "passive"
    ]
    key = email_addr.lower()
    for rec in file_recs:
        if rec["email"].lower() == key:
            rec["email"] = email_addr
            rec["name"] = name
            save_external_recipients(file_recs)
            print(f"Updated external recipient: {email_addr} ({name})")
            return rec
    rec = {"email": email_addr, "name": name}
    file_recs.append(rec)
    save_external_recipients(file_recs)
    print(f"Added external recipient: {email_addr} ({name})")
    return rec


def _bump_external_week_counter(schedule: dict, email_addr: str) -> None:
    week = _week_start_utc()
    raw = schedule.get("external_week")
    if not isinstance(raw, dict) or raw.get("week_start") != week:
        raw = {"week_start": week, "counts": {}}
    counts = raw.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    key = (email_addr or "").strip().lower()
    counts[key] = int(counts.get(key) or 0) + 1
    raw["counts"] = counts
    schedule["external_week"] = raw


def _record_matrix_event(
    schedule: dict,
    *,
    sender: str,
    recipient: str,
    kind: str,
) -> None:
    now = _utc_now()
    events = schedule.get("matrix_events")
    if not isinstance(events, list):
        events = []
    cross = provider_of(sender) != provider_of(recipient)
    events.append(
        {
            "ts": now.isoformat(),
            "from": sender.lower(),
            "to": recipient.lower(),
            "kind": kind,
            "cross": cross,
        }
    )
    # Keep ~7 days of events for matrix / health
    cutoff = now - timedelta(days=7)
    pruned: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = _parse_iso(ev.get("ts"))
        if ts and ts >= cutoff:
            pruned.append(ev)
    schedule["matrix_events"] = pruned[-2000:]

    stats = schedule.get("account_stats")
    if not isinstance(stats, dict):
        stats = {}
    for key, field in ((sender.lower(), "sent"), (recipient.lower(), "received")):
        row = stats.get(key) if isinstance(stats.get(key), dict) else {}
        row[field] = int(row.get(field) or 0) + 1
        row["last_activity"] = now.isoformat()
        stats[key] = row
    schedule["account_stats"] = stats

    last_pairs = schedule.get("last_pairs")
    if not isinstance(last_pairs, dict):
        last_pairs = {}
    last_pairs[sender.lower()] = recipient.lower()
    schedule["last_pairs"] = last_pairs

    total = int(schedule.get("total_sent") or 0)
    if total > 0 and total % 100 == 0:
        print(f"warmup milestone: {total} total warmup emails sent")


def _events_since(schedule: dict, hours: float = 24.0) -> list[dict]:
    cutoff = _utc_now() - timedelta(hours=hours)
    out: list[dict] = []
    for ev in schedule.get("matrix_events") or []:
        if not isinstance(ev, dict):
            continue
        ts = _parse_iso(ev.get("ts"))
        if ts and ts >= cutoff:
            out.append(ev)
    return out


def mix_internal_external_pairs(
    internal_pairs: list[tuple[str, str]],
    external: list[dict],
    *,
    ratio: float | None = None,
) -> list[tuple[str, str, str, str]]:
    """Return (sender, recipient, kind, recip_name) with ~ratio external.

    kind is ``internal`` or ``external``. Internal rotation pairs are kept
    for non-external slots; external slots pick a random external address.
    """
    if ratio is None:
        ratio = effective_external_ratio()
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
    if not isinstance(data.get("matrix_events"), list):
        data["matrix_events"] = []
    if not isinstance(data.get("account_stats"), dict):
        data["account_stats"] = {}
    if not isinstance(data.get("last_pairs"), dict):
        data["last_pairs"] = {}
    if not isinstance(data.get("processed_threads"), list):
        data["processed_threads"] = []
    if data.get("calendar_date") is not None:
        data["calendar_date"] = str(data.get("calendar_date") or "") or None
    if not isinstance(data.get("external_week"), dict):
        data["external_week"] = {"week_start": _week_start_utc(), "counts": {}}
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
    payload = _normalize_schedule(data)
    # Persist extended diagnostics (do not drop unknown keys we care about)
    for key in (
        "external_week",
        "matrix_events",
        "account_stats",
        "last_pairs",
        "processed_threads",
        "calendar_date",
        "warmup_milestones",
    ):
        if key in data:
            payload[key] = data[key]
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
    """Advance day / reset daily counters once per UTC calendar date."""
    now = _utc_now()
    today = now.date().isoformat()
    if schedule.get("calendar_date") == today:
        return schedule

    prev = schedule.get("calendar_date")
    days_elapsed = 0
    if prev:
        try:
            days_elapsed = (now.date() - date.fromisoformat(str(prev))).days
        except ValueError:
            days_elapsed = 0
    else:
        last = _last_activity(schedule)
        if last is not None:
            days_elapsed = (now.date() - last.date()).days

    if days_elapsed > 0:
        schedule["day"] = int(schedule["day"]) + days_elapsed
        schedule["runs_today"] = 0
        schedule["emails_sent_today"] = 0
        d = int(schedule["day"])
        print(
            f"warmup: new UTC day — advanced to day {d} "
            f"({warmup_phase_label(d)}, "
            f"base {max_runs_per_day(d)} runs/day)"
        )
    elif schedule.get("calendar_date") != today:
        # First stamp today without advancing (or same-day migration)
        schedule["runs_today"] = int(schedule.get("runs_today") or 0)
        schedule["emails_sent_today"] = int(schedule.get("emails_sent_today") or 0)

    schedule["calendar_date"] = today
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
    """One send run: one email FROM each warmup account (~70% internal / ~30% external)."""
    accounts = ordered_warmup_accounts()
    n_acc = len(accounts)
    if n_acc < 2:
        print(
            "warmup send error: need at least 2 warmup SMTP accounts "
            "(Zoho SMTP_ACCOUNTS and/or GOOGLE_SMTP_ACCOUNTS)"
        )
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "internal_sent": 0,
            "external_sent": 0,
        }

    emails = [e for e, _ in accounts]
    print(f"warmup accounts ({n_acc}): {', '.join(emails)}")
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

    # Warn if any account silent >6h
    stats = schedule.get("account_stats") if isinstance(schedule.get("account_stats"), dict) else {}
    now = _utc_now()
    for email in emails:
        row = stats.get(email.lower()) if isinstance(stats.get(email.lower()), dict) else {}
        last = _parse_iso(row.get("last_activity"))
        if last and (now - last) > timedelta(hours=6):
            hours = (now - last).total_seconds() / 3600.0
            print(f"WARN: {email} inactive in warmup for {hours:.1f}h")
        elif not last and int(schedule.get("total_sent") or 0) > 0:
            print(f"WARN: {email} has no warmup activity recorded yet")

    last_pairs = schedule.get("last_pairs") if isinstance(schedule.get("last_pairs"), dict) else {}
    mixed = build_cross_warmup_pairs(
        emails,
        last_pairs=last_pairs,
        include_external=True,
    )
    if len(mixed) < 2:
        print("warmup send error: could not build cross-provider pairs")
        return {
            "sent": 0,
            "failed": 0,
            "skipped": True,
            "internal_sent": 0,
            "external_sent": 0,
        }

    n_ext_planned = sum(1 for _s, _r, kind, _n in mixed if kind == "external")
    n_cross = sum(
        1
        for s, r, _k, _n in mixed
        if provider_of(s) != provider_of(r)
    )
    n_int_planned = len(mixed) - n_ext_planned
    print(
        f"warmup send: cross-provider pairs "
        f"(internal={n_int_planned} external={n_ext_planned} "
        f"cross={n_cross}/{len(mixed)})"
    )
    for sender, recipient, kind, recip_name in mixed:
        cross = provider_of(sender) != provider_of(recipient)
        tag = "passive external" if is_passive_external(recipient) else (
            "cross-provider" if cross else kind
        )
        print(f"  planned [{tag}]: {sender} -> {recipient}")

    sent = 0
    failed = 0
    internal_sent = 0
    external_sent = 0

    for i, (sender, recipient, kind, recip_name) in enumerate(mixed):
        if is_passive_external(sender) or sender.lower() in set(passive_external_emails()):
            print(f"warmup SAFETY skip: refusing passive as sender {sender}")
            failed += 1
            continue
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
            skip_throttle=True,
            extra_headers={WARMUP_HEADER: header_val},
        )
        if ok:
            sent += 1
            schedule["emails_sent_today"] = int(schedule["emails_sent_today"]) + 1
            schedule["total_sent"] = int(schedule["total_sent"]) + 1
            cross = provider_of(sender) != provider_of(recipient)
            _record_matrix_event(
                schedule, sender=sender, recipient=recipient, kind=kind
            )
            if kind == "external":
                external_sent += 1
                schedule["external_sent"] = int(schedule.get("external_sent") or 0) + 1
                _bump_external_week_counter(schedule, recipient)
                if is_passive_external(recipient):
                    print(f"Warmup → {recipient} (passive external)")
            else:
                internal_sent += 1
                schedule["internal_sent"] = int(schedule.get("internal_sent") or 0) + 1
            schedule["last_send"] = _iso_now()
            label = "cross-provider" if cross else kind
            print(
                f"Warmup: {sender} → {recipient} ({label}) "
                f"[{sent}/{n_acc}] subject={subject!r}"
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
    from .smtp_accounts import imap_endpoint

    host, port = imap_endpoint(user)
    print(f"warmup IMAP connect {user} via {host}:{port}")
    mail = imaplib.IMAP4_SSL(host, port)
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


def _thread_keys_for_message(msg: email.message.Message) -> list[str]:
    keys: list[str] = []
    for header in ("Message-ID", "In-Reply-To"):
        raw = (msg.get(header) or "").strip()
        if raw:
            keys.append(raw)
    return keys


def _process_account_inbox(
    account_email: str,
    password: str,
    peer_emails: set[str],
    *,
    processed_threads: set[str],
    delay_between: bool = True,
) -> tuple[int, set[str]]:
    """Mark/read/star/reply/file unread peer mail. Returns (replies, new thread ids)."""
    processed = 0
    newly_seen: set[str] = set()
    mail = None
    try:
        mail = _imap_connect(account_email, password)
        typ, _ = mail.select("INBOX")
        if typ != "OK":
            print(f"warmup IMAP: cannot select INBOX for {account_email}")
            return 0, newly_seen

        folder = _ensure_folder(mail, WARMUP_FOLDER)
        mail.select("INBOX")
        unique_ids = _collect_unread_peer_ids(mail, account_email, peer_emails)

        if not unique_ids:
            print(f"warmup IMAP: no unread peer mail in {account_email}")
            return 0, newly_seen

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
                if is_passive_external(from_addr):
                    mail.store(num, "+FLAGS", "(\\Seen)")
                    continue

                warmup_flag = (msg.get(WARMUP_HEADER) or "").strip().lower()
                thread_keys = _thread_keys_for_message(msg)
                already = any(
                    k in processed_threads or k in newly_seen for k in thread_keys
                )

                # Skip our own warmup replies — prevents A↔B reply loops
                if warmup_flag == "reply" or already:
                    mail.store(num, "+FLAGS", "(\\Seen \\Flagged)")
                    try:
                        mail.copy(num, folder)
                        mail.store(num, "+FLAGS", "(\\Deleted)")
                    except Exception:
                        pass
                    for k in thread_keys:
                        newly_seen.add(k)
                    print(
                        f"warmup skip loop-guard: {from_addr} -> {account_email} "
                        f"(header={warmup_flag or 'none'}, seen={already})"
                    )
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
                        skip_throttle=True,
                        extra_headers=extra,
                    )

                if ok:
                    processed += 1
                    for k in thread_keys:
                        newly_seen.add(k)
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
    return processed, newly_seen


def process_warmup_inbox() -> dict:
    """IMAP: read/star/reply/file unread peer mail across all warmup accounts."""
    accounts = ordered_warmup_accounts()
    if len(accounts) < 2:
        print(
            "warmup process error: need at least 2 warmup SMTP accounts "
            "(Zoho SMTP_ACCOUNTS and/or GOOGLE_SMTP_ACCOUNTS)"
        )
        return {"replies": 0, "skipped": True}

    peers = _account_set(accounts)
    schedule = _roll_calendar_day(load_schedule(create_if_missing=True))
    seen = {
        str(x).strip()
        for x in (schedule.get("processed_threads") or [])
        if str(x).strip()
    }
    total = 0
    for email_addr, password in accounts:
        n, new_ids = _process_account_inbox(
            email_addr, password, peers, processed_threads=seen
        )
        total += n
        seen.update(new_ids)

    schedule["processed_threads"] = list(seen)[-500:]
    schedule["total_replies"] = int(schedule.get("total_replies") or 0) + total
    schedule["last_process"] = _iso_now()
    save_schedule(schedule)
    print(f"warmup process done: {total} replies total this run")
    try:
        from .bounce_handler import scan_all_bounces

        scan_all_bounces(days=7)
    except Exception as e:
        print(f"bounce scan after warmup process skipped: {e}")
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


def _short_label(email_addr: str) -> str:
    key = (email_addr or "").strip().lower()
    if is_passive_external(key) or key.endswith("@gmail.com"):
        if "trestov" in key:
            return "ext"
        local = _local_part(key)[:6] or "ext"
        return local[:7]
    local = _local_part(key)
    domain = key.split("@")[-1] if "@" in key else ""
    if "meet-agency" in domain:
        mapping = {
            "vladislav": "gv",
            "founder": "gf",
            "outreach": "go",
        }
        return mapping.get(local, f"g-{local[:4]}")[:7]
    zoho_map = {
        "vladislav": "vladis",
        "outreach": "outrea",
        "founder": "foundr",
        "hello": "hello",
        "vlad": "vlad",
    }
    if local in zoho_map:
        return zoho_map[local]
    return (local or key[:7])[:7]


def list_warmup_account_emails() -> list[str]:
    """All enabled warmup accounts from accounts.json (no password required)."""
    from .smtp_accounts import load_account_records

    records = load_account_records(
        enabled_only=True, cold_only=False, with_password=False, warmup_only=True
    )
    emails = [a["email"] for a in records]
    zoho: list[str] = []
    google: list[str] = []
    other: list[str] = []
    by_local_zoho = {
        _local_part(e): e
        for e in emails
        if "dentalmeet" in e.lower()
    }
    for alias in ACCOUNT_ORDER:
        if alias in by_local_zoho:
            zoho.append(by_local_zoho[alias])
    seen = {e.lower() for e in zoho}
    for e in emails:
        if e.lower() in seen:
            continue
        if "meet-agency" in e.lower() or provider_of(e) == "google":
            google.append(e)
        else:
            other.append(e)
        seen.add(e.lower())
    return zoho + google + other


def print_warmup_matrix(*, hours: float = 24.0) -> None:
    schedule = load_schedule(create_if_missing=True)
    events = _events_since(schedule, hours=hours)
    ordered = list_warmup_account_emails()
    cols = list(ordered) + [
        r["email"]
        for r in load_external_recipients()
        if r["email"].lower() not in {x.lower() for x in ordered}
    ]
    labels = [_short_label(e) for e in cols]
    idx = {e.lower(): i for i, e in enumerate(cols)}

    n = len(cols)
    grid = [[0 for _ in range(n)] for _ in range(n)]
    cross = 0
    for ev in events:
        fi = idx.get((ev.get("from") or "").lower())
        ti = idx.get((ev.get("to") or "").lower())
        if fi is None or ti is None:
            continue
        grid[fi][ti] += 1
        if ev.get("cross"):
            cross += 1

    print(f"=== WARMUP MATRIX (last {int(hours)}h) ===")
    header = f"{'':12}" + "".join(f"{lab:>8}" for lab in labels)
    print(header)
    for i, _email in enumerate(cols):
        row = f"{labels[i]:12}"
        for j in range(n):
            row += f"{'-':>8}" if i == j else f"{grid[i][j]:>8}"
        print(row)
    total = len(events)
    ratio = (100.0 * cross / total) if total else 0.0
    print(f"\nTotal: {total} warmup emails in {int(hours)}h")
    print(f"Cross-provider: {ratio:.0f}%")
    print("==============================")


def print_warmup_health() -> None:
    schedule = load_schedule(create_if_missing=True)
    emails = list_warmup_account_emails()
    events = _events_since(schedule, hours=24.0)
    now = _utc_now()
    stats = schedule.get("account_stats") if isinstance(schedule.get("account_stats"), dict) else {}

    active = 0
    inactive: list[str] = []
    for email in emails:
        row = stats.get(email.lower()) if isinstance(stats.get(email.lower()), dict) else {}
        last = _parse_iso(row.get("last_activity"))
        if not last:
            for ev in reversed(events):
                if ev.get("from") == email.lower() or ev.get("to") == email.lower():
                    last = _parse_iso(ev.get("ts"))
                    break
        if last and (now - last) <= timedelta(hours=6):
            active += 1
        else:
            inactive.append(email)
            if last:
                hours = (now - last).total_seconds() / 3600.0
                print(f"WARN: {email} inactive {hours:.1f}h")
            else:
                print(f"WARN: {email} no warmup activity recorded yet")

    cross = sum(1 for ev in events if ev.get("cross"))
    ratio = (100.0 * cross / len(events)) if events else 0.0
    passive_ok = bool(passive_external_emails())
    ext_recent = any(is_passive_external(ev.get("to") or "") for ev in events)

    last_send = _parse_iso(schedule.get("last_send"))
    if last_send:
        ago_m = int((now - last_send).total_seconds() // 60)
        last_label = f"{ago_m} min ago" if ago_m < 120 else f"{ago_m / 60:.1f}h ago"
        last_ok = (now - last_send) <= timedelta(hours=6)
    else:
        last_label = "never"
        last_ok = False

    print("=== WARMUP HEALTH ===")
    print(f"Total accounts: {len(emails)}")
    mark = "OK" if active == len(emails) and emails else "WARN"
    print(f"Active in warmup: {active}/{len(emails)} {mark}")
    cross_mark = "OK" if (ratio >= 50 or not events) else "WARN"
    print(f"Cross-provider ratio: {ratio:.0f}% {cross_mark}")
    ext_mark = "OK" if passive_ok else "WARN"
    ext_state = "active" if ext_recent else ("configured" if passive_ok else "missing")
    print(
        f"External warmup: {ext_state} {ext_mark} "
        f"({', '.join(passive_external_emails()) or 'none'})"
    )
    print(f"Last activity: {last_label} {'OK' if last_ok else 'WARN'}")
    if inactive:
        print(f"Inactive: {', '.join(inactive)}")
    print("=====================")


def test_smtp_account(email_addr: str, *, send_probe: bool = True) -> dict:
    """Test SMTP login for one accounts.json mailbox."""
    from .smtp_accounts import get_account, resolve_account_password, smtp_endpoint

    email_addr = (email_addr or "").strip()
    print(f"=== SMTP TEST: {email_addr} ===")
    acc = get_account(email_addr)
    if not acc or is_passive_external(email_addr):
        print("FAIL: address not in accounts.json (or is passive external)")
        print("Fix: add enabled account to data/accounts.json with password_env")
        print("===========================")
        return {"ok": False, "error": "not_in_accounts"}

    env_key = (acc.get("password_env") or "").strip()
    pwd = resolve_account_password(acc)
    print(f"provider: {acc.get('provider')}")
    print(f"password_env: {env_key or '(none)'}")
    if env_key == "GOOGLE_SMTP_VLADISLAV_PASSWORD":
        print("alias: also accepts GOOGLE_SMTP_1")
    if not pwd:
        print(f"FAIL: Authentication — no password in env ({env_key})")
        print(
            f"Fix: set GitHub secret / .env {env_key} "
            "(or GOOGLE_SMTP_1 / SMTP_* CSV lists)"
        )
        print("===========================")
        return {"ok": False, "error": "no_password"}

    host, port, provider = smtp_endpoint(email_addr)
    print(f"SMTP endpoint: {host}:{port} ({provider})")
    try:
        import smtplib

        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            print("SMTP connection: OK")
            smtp.login(email_addr, pwd)
            print("Authentication: OK")
            if send_probe:
                from email.mime.text import MIMEText

                msg = MIMEText("DentalMeet SMTP probe — ignore.")
                msg["Subject"] = "[DentalMeet] SMTP probe"
                msg["From"] = email_addr
                msg["To"] = email_addr
                smtp.sendmail(email_addr, [email_addr], msg.as_string())
                print("Test send: OK")
            else:
                print("Test send: skipped (pass send_probe=True)")
        print("===========================")
        return {"ok": True}
    except Exception as e:
        print(f"FAIL: {e}")
        print("Fix: check app password, IMAP/SMTP enabled, and firewall/DNS")
        print("===========================")
        return {"ok": False, "error": str(e)}


def show_stats() -> None:
    """Print human-readable warmup stats; create schedule file if missing."""
    schedule = _roll_calendar_day(load_schedule(create_if_missing=True))
    save_schedule(schedule)
    external = load_external_recipients()
    passive = load_passive_external_recipients()

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
    print(
        f"External pool: {len(external)} "
        f"(passive={len(passive)}, file={len(external) - len(passive)}, "
        f"ratio={effective_external_ratio():.0%})"
    )
    week = schedule.get("external_week") if isinstance(schedule.get("external_week"), dict) else {}
    week_start = week.get("week_start") or _week_start_utc()
    counts = week.get("counts") if isinstance(week.get("counts"), dict) else {}
    if week.get("week_start") != _week_start_utc():
        counts = {}
        week_start = _week_start_utc()
    print(f"External sent this week (since {week_start}):")
    for rec in external:
        key = rec["email"].lower()
        n = int(counts.get(key) or 0)
        role = rec.get("role") or "external"
        print(f"  - {rec['email']} [{role}]: {n}")

    print("\nPer-account warmup:")
    now = _utc_now()
    account_stats = (
        schedule.get("account_stats")
        if isinstance(schedule.get("account_stats"), dict)
        else {}
    )
    for email in list_warmup_account_emails():
        row = account_stats.get(email.lower()) if isinstance(
            account_stats.get(email.lower()), dict
        ) else {}
        sent_n = int(row.get("sent") or 0)
        recv_n = int(row.get("received") or 0)
        last = _parse_iso(row.get("last_activity"))
        last_s = _format_ts(row.get("last_activity")) if last else "never"
        active = bool(last and (now - last) <= timedelta(hours=6))
        mark = "OK" if active else "inactive"
        print(
            f"  {email}\n"
            f"    sent FROM: {sent_n}  received BY: {recv_n}\n"
            f"    last activity: {last_s}  ACTIVE in warmup: {mark}"
        )
    for rec in passive:
        email = rec["email"]
        row = account_stats.get(email.lower()) if isinstance(
            account_stats.get(email.lower()), dict
        ) else {}
        print(
            f"  {email} [passive recipient-only]\n"
            f"    sent FROM: 0  received BY: {int(row.get('received') or 0)}\n"
            f"    last activity: {_format_ts(row.get('last_activity'))}"
        )

    print(f"\nLast send: {_format_ts(schedule.get('last_send'))}")
    print(f"Last process: {_format_ts(schedule.get('last_process'))}")
    print(f"Next warmup: {_next_warmup_label(schedule.get('last_send'))}")
    print(f"Status: {_status_label(day)}")
    try:
        from .bounce_handler import print_bounce_stats

        print_bounce_stats()
    except Exception as e:
        print(f"bounce stats unavailable: {e}")
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
        description="Mailbox warmup (Zoho + Google cross-provider + external)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "send",
        help="Send one warmup run (1 email FROM each account; ~70%% cross-provider)",
    )
    sub.add_parser("process", help="IMAP: reply/star/file unread peer mail")
    sub.add_parser("stats", help="Show warmup schedule + per-account stats")
    sub.add_parser("reset", help="Reset schedule to day 1")
    sub.add_parser("matrix", help="Print from→to warmup matrix (last 24h)")
    sub.add_parser("health", help="Warmup health check across all accounts")
    sub.add_parser(
        "list-external",
        help="List passive + file external warmup recipients (receiver-only)",
    )

    p_add = sub.add_parser(
        "add-external",
        help="Add an external warmup recipient to data/external_recipients.json",
    )
    p_add.add_argument("--email", required=True, help="External email address")
    p_add.add_argument("--name", default="", help="Display name")

    p_smtp = sub.add_parser("test-smtp", help="Test SMTP for one account email")
    p_smtp.add_argument("--email", required=True)
    p_smtp.add_argument(
        "--no-send",
        action="store_true",
        help="Skip self-send probe (login only)",
    )

    args = parser.parse_args(argv)
    from .alerts import bump_daily_metric, run_monitored

    if args.command == "send":
        result = run_monitored("warmup_send", send_warmup_emails)
        sent = int((result or {}).get("sent") or 0)
        if sent:
            bump_daily_metric("warmup_emails", sent)
    elif args.command == "process":
        run_monitored("warmup_process", process_warmup_inbox)
    elif args.command == "stats":
        run_monitored("warmup_stats", show_stats)
    elif args.command == "reset":
        reset_schedule()
    elif args.command == "matrix":
        print_warmup_matrix()
    elif args.command == "health":
        print_warmup_health()
    elif args.command == "list-external":
        list_external_recipients()
    elif args.command == "add-external":
        add_external_recipient(args.email, args.name)
    elif args.command == "test-smtp":
        test_smtp_account(args.email, send_probe=not args.no_send)


if __name__ == "__main__":
    main()
