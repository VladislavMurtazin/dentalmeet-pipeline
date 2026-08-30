"""Dynamic per-account send limits and health-weighted SMTP rotation."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS_PATH = ROOT / "data" / "account_metrics.json"

BASE_DAILY_LIMIT = 5
MAX_DAILY_LIMIT = 20
HEALTHY_OPEN_RATE = 60.0  # percent
LOW_SPAM_PCT = 0.1  # <0.1%
HIGH_SPAM_PCT = 0.3  # >0.3%
HIGH_BOUNCE_PCT = 2.0  # >2%
COOLDOWN_BOUNCE_PCT = 3.0  # >3% → 24h cooldown
SPAM_COOLDOWN_HOURS = 48
BOUNCE_COOLDOWN_HOURS = 24
EVENT_RETENTION_DAYS = 30
RECIPIENT_MAP_DAYS = 45


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utc_now().date().isoformat()


def _load_store() -> dict:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not METRICS_PATH.exists():
        return {"accounts": {}, "recipient_map": {}}
    try:
        raw = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"accounts": {}, "recipient_map": {}}
        raw.setdefault("accounts", {})
        raw.setdefault("recipient_map", {})
        return raw
    except (OSError, json.JSONDecodeError):
        return {"accounts": {}, "recipient_map": {}}


def _save_store(data: dict) -> None:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _account_entry(store: dict, account: str) -> dict:
    accounts = store.setdefault("accounts", {})
    key = (account or "").strip().lower()
    entry = accounts.get(key)
    if not isinstance(entry, dict):
        entry = {
            "events": [],
            "sent_today": 0,
            "sent_today_date": _today(),
            "cooldown_until": None,
            "current_daily_limit": BASE_DAILY_LIMIT,
            "last_health_alert_at": None,
            "last_cooldown_alert_at": None,
        }
        accounts[key] = entry
    entry.setdefault("events", [])
    entry.setdefault("sent_today", 0)
    entry.setdefault("sent_today_date", _today())
    entry.setdefault("cooldown_until", None)
    entry.setdefault("current_daily_limit", BASE_DAILY_LIMIT)
    return entry


def _prune_events(events: list, *, days: int = EVENT_RETENTION_DAYS) -> list:
    cutoff = (_utc_now() - timedelta(days=days)).date().isoformat()
    out = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("date") or "") >= cutoff:
            out.append(ev)
    return out


def _prune_recipient_map(store: dict) -> None:
    cutoff = (_utc_now() - timedelta(days=RECIPIENT_MAP_DAYS)).date().isoformat()
    rmap = store.get("recipient_map") or {}
    if not isinstance(rmap, dict):
        store["recipient_map"] = {}
        return
    store["recipient_map"] = {
        k: v
        for k, v in rmap.items()
        if isinstance(v, dict) and str(v.get("date") or "") >= cutoff
    }


def _reset_daily_if_needed(entry: dict) -> None:
    today = _today()
    if entry.get("sent_today_date") != today:
        entry["sent_today"] = 0
        entry["sent_today_date"] = today


@dataclass
class AccountMetrics:
    account: str
    sent_30d: int = 0
    opened_30d: int = 0
    replied_30d: int = 0
    bounced_30d: int = 0
    spam_complaints_30d: int = 0
    open_rate: float = 0.0  # percent
    bounce_rate: float = 0.0  # percent
    spam_rate: float = 0.0  # percent
    reply_rate: float = 0.0  # percent
    current_daily_limit: int = BASE_DAILY_LIMIT
    sent_today: int = 0
    cooldown_until: str | None = None
    health_score: float = 100.0

    @property
    def in_cooldown(self) -> bool:
        if not self.cooldown_until:
            return False
        try:
            until = datetime.fromisoformat(self.cooldown_until.replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return _utc_now() < until
        except ValueError:
            return False


def get_account_metrics(account: str, last_30_days: int = 30) -> AccountMetrics:
    """Aggregate last-N-day metrics for one SMTP account."""
    store = _load_store()
    key = (account or "").strip().lower()
    entry = _account_entry(store, key)
    _reset_daily_if_needed(entry)
    entry["events"] = _prune_events(entry.get("events") or [], days=last_30_days)

    counts = {
        "sent": 0,
        "opened": 0,
        "replied": 0,
        "bounced": 0,
        "spam": 0,
    }
    for ev in entry["events"]:
        et = str(ev.get("type") or "")
        if et in counts:
            counts[et] += 1

    sent = counts["sent"]
    open_rate = round(100.0 * counts["opened"] / sent, 1) if sent else 0.0
    bounce_rate = round(100.0 * counts["bounced"] / sent, 2) if sent else 0.0
    spam_rate = round(100.0 * counts["spam"] / sent, 3) if sent else 0.0
    reply_rate = round(100.0 * counts["replied"] / sent, 1) if sent else 0.0

    m = AccountMetrics(
        account=key,
        sent_30d=sent,
        opened_30d=counts["opened"],
        replied_30d=counts["replied"],
        bounced_30d=counts["bounced"],
        spam_complaints_30d=counts["spam"],
        open_rate=open_rate,
        bounce_rate=bounce_rate,
        spam_rate=spam_rate,
        reply_rate=reply_rate,
        current_daily_limit=int(entry.get("current_daily_limit") or BASE_DAILY_LIMIT),
        sent_today=int(entry.get("sent_today") or 0),
        cooldown_until=entry.get("cooldown_until"),
    )
    m.health_score = calculate_health_score(m)
    return m


def get_daily_limit(account: str) -> int:
    """Calculate safe daily limit based on account health."""
    metrics = get_account_metrics(account)
    base_limit = BASE_DAILY_LIMIT

    # Reputation penalties (rates stored as percent)
    if metrics.spam_rate > HIGH_SPAM_PCT:  # >0.3% spam reports
        return max(1, base_limit // 2)

    if metrics.bounce_rate > HIGH_BOUNCE_PCT:  # >2% bounces
        return max(1, base_limit // 2)

    if metrics.open_rate > HEALTHY_OPEN_RATE and metrics.spam_rate < LOW_SPAM_PCT:
        # Healthy account, can scale up
        return min(MAX_DAILY_LIMIT, base_limit + 5)

    # Gradual scale for established accounts with decent volume and low bounce
    if metrics.sent_30d >= 50 and metrics.bounce_rate < 1.0 and metrics.spam_rate < LOW_SPAM_PCT:
        return min(MAX_DAILY_LIMIT, base_limit + 3)

    return base_limit


def calculate_health_score(account_or_metrics: str | AccountMetrics) -> float:
    """0–100 health score used for weighted rotation."""
    if isinstance(account_or_metrics, AccountMetrics):
        metrics = account_or_metrics
    else:
        metrics = get_account_metrics(account_or_metrics)

    # Convert percent rates → fractions for the scoring formula
    spam_frac = metrics.spam_rate / 100.0
    bounce_frac = metrics.bounce_rate / 100.0

    score = 100.0
    score -= spam_frac * 10000  # Heavy penalty for spam
    score -= bounce_frac * 500  # Penalty for bounces
    score += metrics.open_rate * 0.5  # Bonus for good open rate (percent)
    score += metrics.reply_rate * 2  # Bonus for replies (percent)

    if metrics.in_cooldown:
        score = min(score, 10.0)

    return max(0.0, min(100.0, round(score, 1)))


def _set_cooldown(store: dict, account: str, hours: int, reason: str) -> str:
    entry = _account_entry(store, account)
    until = _utc_now() + timedelta(hours=hours)
    until_iso = until.isoformat(timespec="seconds")
    # Don't shorten an existing longer cooldown
    existing = entry.get("cooldown_until")
    if existing:
        try:
            prev = datetime.fromisoformat(str(existing).replace("Z", "+00:00"))
            if prev.tzinfo is None:
                prev = prev.replace(tzinfo=timezone.utc)
            if prev > until:
                return str(existing)
        except ValueError:
            pass
    entry["cooldown_until"] = until_iso
    print(f"{account} in cooldown until {until.date().isoformat()} ({reason})")
    _maybe_alert_cooldown(entry, account, until.date().isoformat(), reason)
    return until_iso


def _maybe_alert_cooldown(entry: dict, account: str, until_date: str, reason: str) -> None:
    today = _today()
    if entry.get("last_cooldown_alert_at") == today:
        return
    entry["last_cooldown_alert_at"] = today
    try:
        from .alerts import send_alert

        send_alert(
            f"Account cooldown: {account}",
            (
                f"{account} entered send cooldown until {until_date}.\n"
                f"Reason: {reason}\n"
                f"Other accounts will handle outbound load."
            ),
            severity="warning",
        )
    except Exception as e:
        print(f"cooldown alert failed: {e}")


def _maybe_alert_low_health(entry: dict, account: str, score: float) -> None:
    if score >= 50:
        return
    today = _today()
    if entry.get("last_health_alert_at") == today:
        return
    entry["last_health_alert_at"] = today
    try:
        from .alerts import send_alert

        send_alert(
            f"Low account health: {account} ({score})",
            (
                f"Health score for {account} dropped to {score} (threshold 50).\n"
                f"Review bounce/spam rates and reduce volume if needed."
            ),
            severity="warning",
        )
    except Exception as e:
        print(f"health alert failed: {e}")


def refresh_account_limits() -> dict:
    """Recompute daily limits + auto-cooldown from bounce rate; persist summary."""
    from .email_sender import load_smtp_accounts

    store = _load_store()
    _prune_recipient_map(store)
    summary: dict[str, dict] = {}

    for email, _ in load_smtp_accounts():
        key = email.lower()
        entry = _account_entry(store, key)
        _reset_daily_if_needed(entry)
        entry["events"] = _prune_events(entry.get("events") or [])

        metrics = get_account_metrics(key)
        limit = get_daily_limit(key)
        entry["current_daily_limit"] = limit

        # Auto cooldown on high bounce rate
        if metrics.bounce_rate > COOLDOWN_BOUNCE_PCT and metrics.sent_30d >= 10:
            if not metrics.in_cooldown:
                _set_cooldown(
                    store,
                    key,
                    BOUNCE_COOLDOWN_HOURS,
                    f"bounce_rate {metrics.bounce_rate}% > {COOLDOWN_BOUNCE_PCT}%",
                )

        metrics = get_account_metrics(key)
        metrics.current_daily_limit = limit
        score = calculate_health_score(metrics)
        _maybe_alert_low_health(entry, key, score)

        summary[key] = {
            "sent_30d": metrics.sent_30d,
            "opened_30d": metrics.opened_30d,
            "replied_30d": metrics.replied_30d,
            "bounced_30d": metrics.bounced_30d,
            "spam_complaints_30d": metrics.spam_complaints_30d,
            "open_rate": metrics.open_rate,
            "bounce_rate": metrics.bounce_rate,
            "spam_rate": metrics.spam_rate,
            "reply_rate": metrics.reply_rate,
            "current_daily_limit": limit,
            "sent_today": metrics.sent_today,
            "health_score": score,
            "cooldown_until": entry.get("cooldown_until"),
        }
        # Mirror summary fields onto entry for easy JSON inspection
        for k, v in summary[key].items():
            if k != "cooldown_until":
                entry[k] = v

    _save_store(store)
    return summary


def is_account_available(account: str) -> bool:
    """True if account is not in cooldown and under today's limit."""
    metrics = get_account_metrics(account)
    if metrics.in_cooldown:
        return False
    limit = get_daily_limit(account)
    return metrics.sent_today < limit


def remaining_capacity(account: str) -> int:
    metrics = get_account_metrics(account)
    if metrics.in_cooldown:
        return 0
    limit = get_daily_limit(account)
    return max(0, limit - metrics.sent_today)


def any_account_available() -> bool:
    from .email_sender import load_smtp_accounts

    return any(is_account_available(e) for e, _ in load_smtp_accounts())


def total_remaining_capacity() -> int:
    from .email_sender import load_smtp_accounts

    return sum(remaining_capacity(e) for e, _ in load_smtp_accounts())


def select_next_account(
    accounts: list[tuple[str, str]] | None = None,
    *,
    refresh: bool = False,
) -> tuple[str, str, int] | None:
    """Select account by health-weighted random among available accounts."""
    from .email_sender import load_smtp_accounts

    accounts = accounts if accounts is not None else load_smtp_accounts()
    if not accounts:
        return None

    if refresh:
        refresh_account_limits()

    candidates: list[tuple[str, str, int, float]] = []
    for idx, (email, password) in enumerate(accounts):
        if not is_account_available(email):
            metrics = get_account_metrics(email)
            if metrics.in_cooldown:
                until = metrics.cooldown_until or "?"
                try:
                    d = datetime.fromisoformat(until.replace("Z", "+00:00")).date().isoformat()
                except ValueError:
                    d = until
                print(f"{email} in cooldown until {d}")
            continue
        score = max(0.1, calculate_health_score(email))
        candidates.append((email, password, idx, score))

    if not candidates:
        print("All accounts at capacity, queued for tomorrow")
        _alert_all_at_capacity()
        return None

    return weighted_random_choice(candidates)


def weighted_random_choice(
    candidates: list[tuple[str, str, int, float]],
) -> tuple[str, str, int]:
    """Weighted random selection (healthier = more likely)."""
    weights = [c[3] for c in candidates]
    total = sum(weights)
    if total <= 0:
        pick = random.choice(candidates)
        return pick[0], pick[1], pick[2]
    r = random.uniform(0, total)
    upto = 0.0
    for email, password, idx, score in candidates:
        upto += score
        if upto >= r:
            return email, password, idx
    email, password, idx, _ = candidates[-1]
    return email, password, idx


def _alert_all_at_capacity() -> None:
    store = _load_store()
    meta = store.setdefault("_meta", {})
    today = _today()
    if meta.get("last_capacity_alert_at") == today:
        _save_store(store)
        return
    meta["last_capacity_alert_at"] = today
    _save_store(store)
    try:
        from .alerts import send_alert

        send_alert(
            "All SMTP accounts at capacity",
            (
                "Every sending account is either at its daily limit or in cooldown.\n"
                "Outbound is throttled; remaining leads stay queued for tomorrow."
            ),
            severity="warning",
        )
    except Exception as e:
        print(f"capacity alert failed: {e}")


def record_send(account: str, recipient: str | None = None) -> None:
    """Record a successful outbound send against an account's daily + 30d stats."""
    store = _load_store()
    key = (account or "").strip().lower()
    entry = _account_entry(store, key)
    _reset_daily_if_needed(entry)
    today = _today()
    entry["events"] = _prune_events(entry.get("events") or [])
    entry["events"].append({"type": "sent", "date": today})
    entry["sent_today"] = int(entry.get("sent_today") or 0) + 1

    if recipient:
        r = recipient.strip().lower()
        store.setdefault("recipient_map", {})[r] = {
            "account": key,
            "date": today,
        }

    _save_store(store)
    limit = get_daily_limit(key)
    store = _load_store()
    entry = _account_entry(store, key)
    entry["current_daily_limit"] = limit
    _save_store(store)
    print(f"{key} sent {entry['sent_today']}/{limit} today")


def _account_for_recipient(store: dict, recipient: str) -> str | None:
    r = (recipient or "").strip().lower()
    hit = (store.get("recipient_map") or {}).get(r)
    if isinstance(hit, dict):
        return (hit.get("account") or "").strip().lower() or None
    return None


def record_bounce_for_recipient(recipient: str) -> None:
    store = _load_store()
    account = _account_for_recipient(store, recipient)
    if not account:
        return
    entry = _account_entry(store, account)
    entry["events"] = _prune_events(entry.get("events") or [])
    entry["events"].append({"type": "bounced", "date": _today()})
    _save_store(store)
    metrics = get_account_metrics(account)
    if metrics.bounce_rate > COOLDOWN_BOUNCE_PCT and metrics.sent_30d >= 5:
        store = _load_store()
        _set_cooldown(
            store,
            account,
            BOUNCE_COOLDOWN_HOURS,
            f"bounce_rate {metrics.bounce_rate}%",
        )
        _save_store(store)


def record_reply_for_recipient(recipient: str) -> None:
    store = _load_store()
    account = _account_for_recipient(store, recipient)
    if not account:
        return
    entry = _account_entry(store, account)
    entry["events"] = _prune_events(entry.get("events") or [])
    entry["events"].append({"type": "replied", "date": _today()})
    _save_store(store)


def record_open_for_recipient(recipient: str) -> None:
    store = _load_store()
    account = _account_for_recipient(store, recipient)
    if not account:
        return
    entry = _account_entry(store, account)
    entry["events"] = _prune_events(entry.get("events") or [])
    entry["events"].append({"type": "opened", "date": _today()})
    _save_store(store)


def record_spam_complaint(account: str, recipient: str | None = None) -> None:
    """Mark spam complaint → 48h cooldown for the account."""
    store = _load_store()
    key = (account or "").strip().lower()
    if recipient and not key:
        key = _account_for_recipient(store, recipient) or ""
    if not key:
        print("record_spam_complaint: unknown account")
        return
    entry = _account_entry(store, key)
    entry["events"] = _prune_events(entry.get("events") or [])
    entry["events"].append({"type": "spam", "date": _today()})
    _set_cooldown(store, key, SPAM_COOLDOWN_HOURS, "spam complaint")
    _save_store(store)


def print_account_health() -> None:
    summary = refresh_account_limits()
    print("=== ACCOUNT HEALTH ===")
    if not summary:
        print("(no SMTP accounts configured)")
        print("======================")
        return
    for email, row in sorted(summary.items(), key=lambda x: -x[1].get("health_score", 0)):
        cd = row.get("cooldown_until") or "-"
        if cd != "-":
            try:
                cd = datetime.fromisoformat(str(cd).replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
        print(
            f"{email}\n"
            f"  health={row.get('health_score')}  "
            f"limit={row.get('current_daily_limit')}  "
            f"today={row.get('sent_today')}/{row.get('current_daily_limit')}  "
            f"cooldown={cd}\n"
            f"  30d: sent={row.get('sent_30d')} open={row.get('open_rate')}% "
            f"reply={row.get('reply_rate')}% bounce={row.get('bounce_rate')}% "
            f"spam={row.get('spam_rate')}%"
        )
    print("======================")


def print_throttle_status() -> None:
    summary = refresh_account_limits()
    print("=== THROTTLE STATUS ===")
    total_left = 0
    for email, row in sorted(summary.items()):
        limit = int(row.get("current_daily_limit") or 0)
        sent = int(row.get("sent_today") or 0)
        left = max(0, limit - sent)
        metrics = get_account_metrics(email)
        if metrics.in_cooldown:
            left = 0
            status = "COOLDOWN"
        elif left <= 0:
            status = "FULL"
        else:
            status = "OK"
        total_left += left
        print(f"  {email}: {sent}/{limit} today  remaining={left}  [{status}]")
    print(f"Total remaining capacity today: {total_left}")
    if total_left <= 0:
        print("All accounts at capacity, queued for tomorrow")
    print("=======================")
