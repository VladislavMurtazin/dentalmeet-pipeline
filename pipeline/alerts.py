"""Pipeline alerting — job failure tracking, health checks, daily summary."""

from __future__ import annotations

import json
import os
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
FAILURE_LOG_PATH = ROOT / "data" / "failure_log.json"
ALERT_STATE_PATH = ROOT / "data" / "alert_state.json"
DAILY_METRICS_PATH = ROOT / "data" / "daily_metrics.json"
SEND_METRICS_PATH = ROOT / "data" / "send_metrics.json"

CONSECUTIVE_FAILURE_THRESHOLD = 3
APOLLO_CREDITS_WARN = 100
WARMUP_STALE_HOURS = 24
BOUNCE_RATE_WARN = 0.03
DAILY_SUMMARY_HOUR_UTC = 20

SEVERITY_PREFIX = {
    "info": "[INFO]",
    "warning": "[WARN]",
    "critical": "[CRITICAL]",
}


def alert_email() -> str:
    return os.environ.get("ALERT_EMAIL", "").strip()


def alert_from_email() -> str:
    return (
        os.environ.get("ALERT_FROM_EMAIL", "").strip()
        or "vladislav@dentalmeet.agency"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return _utc_now().date().isoformat()


def _load_json(path: Path, default: dict | list) -> dict | list:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def send_alert(subject: str, body: str, severity: str = "info") -> bool:
    """Send alert email to ALERT_EMAIL from vladislav@dentalmeet.agency (Zoho)."""
    to = alert_email()
    if not to:
        print("WARN: ALERT_EMAIL not set — alert not sent")
        print(f"  [{severity}] {subject}")
        print(body[:500])
        return False

    prefix = SEVERITY_PREFIX.get(severity.lower(), SEVERITY_PREFIX["info"])
    full_subject = f"{prefix} DentalMeet: {subject}"
    full_body = (
        f"Severity: {severity}\n"
        f"Time (UTC): {_utc_now().isoformat(timespec='seconds')}\n\n"
        f"{body}\n\n"
        f"— DentalMeet pipeline alerts"
    )

    from .email_sender import send_email

    ok = send_email(
        to,
        full_subject,
        full_body,
        from_email=alert_from_email(),
        skip_unsubscribe=True,
        skip_throttle=True,
    )
    if ok:
        print(f"alert sent → {to}: {subject}")
    return ok


def load_failure_log() -> dict:
    raw = _load_json(FAILURE_LOG_PATH, {})
    return raw if isinstance(raw, dict) else {}


def save_failure_log(data: dict) -> None:
    _save_json(FAILURE_LOG_PATH, data)


def _job_entry(data: dict, job_name: str) -> dict:
    entry = data.setdefault(
        job_name,
        {"consecutive_failures": 0, "last_error": None, "last_failure_at": None},
    )
    entry.setdefault("consecutive_failures", 0)
    return entry


def record_job_success(job_name: str) -> None:
    """Reset consecutive failure count on successful job run."""
    data = load_failure_log()
    entry = _job_entry(data, job_name)
    entry["consecutive_failures"] = 0
    entry["last_error"] = None
    entry["last_success_at"] = _utc_now().isoformat(timespec="seconds")
    save_failure_log(data)


def record_job_failure(job_name: str, error: str) -> bool:
    """
    Increment failure count. Returns True if alert was sent (3rd consecutive).
    """
    data = load_failure_log()
    entry = _job_entry(data, job_name)
    entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    entry["last_error"] = (error or "unknown error")[:500]
    entry["last_failure_at"] = _utc_now().isoformat(timespec="seconds")
    save_failure_log(data)

    count = entry["consecutive_failures"]
    print(f"job failure tracked: {job_name} consecutive={count}")
    if count >= CONSECUTIVE_FAILURE_THRESHOLD:
        send_alert(
            f"Job failed {count} times in a row: {job_name}",
            (
                f"The pipeline job '{job_name}' has failed {count} consecutive times.\n\n"
                f"Last error:\n{entry['last_error']}\n\n"
                f"Check GitHub Actions logs and data/failure_log.json."
            ),
            severity="critical",
        )
        entry["consecutive_failures"] = 0
        save_failure_log(data)
        return True
    return False


def run_monitored(job_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a job function; track failures and run health checks on success."""
    try:
        result = fn(*args, **kwargs)
        record_job_success(job_name)
        run_health_checks_for_job(job_name)
        maybe_send_daily_summary()
        return result
    except Exception as e:
        record_job_failure(job_name, f"{type(e).__name__}: {e}")
        raise


def load_alert_state() -> dict:
    raw = _load_json(ALERT_STATE_PATH, {})
    return raw if isinstance(raw, dict) else {}


def save_alert_state(data: dict) -> None:
    _save_json(ALERT_STATE_PATH, data)


def _alert_already_sent(key: str) -> bool:
    state = load_alert_state()
    sent = state.setdefault("alerts_sent", {})
    return sent.get(key) == _today_key()


def _mark_alert_sent(key: str) -> None:
    state = load_alert_state()
    state.setdefault("alerts_sent", {})[key] = _today_key()
    save_alert_state(state)


def load_daily_metrics() -> dict:
    raw = _load_json(DAILY_METRICS_PATH, {})
    return raw if isinstance(raw, dict) else {}


def save_daily_metrics(data: dict) -> None:
    _save_json(DAILY_METRICS_PATH, data)


def bump_daily_metric(key: str, amount: int = 1) -> None:
    """Increment a counter for today (emails_sent, replies, interested, warmup_emails)."""
    if amount <= 0:
        return
    data = load_daily_metrics()
    day = data.setdefault(_today_key(), {})
    day[key] = int(day.get(key) or 0) + amount
    save_daily_metrics(data)


def apollo_credits_remaining() -> int:
    from .credit_budget import agency_remaining, dental_remaining

    return agency_remaining() + dental_remaining()


def check_apollo_credits_low(threshold: int = APOLLO_CREDITS_WARN) -> bool:
    """Alert if combined Apollo budget remaining is below threshold."""
    remaining = apollo_credits_remaining()
    if remaining >= threshold:
        return False
    if _alert_already_sent("apollo_credits_low"):
        return False
    send_alert(
        "Apollo credits below 100",
        (
            f"Combined Apollo credit budget remaining: {remaining} "
            f"(threshold: {threshold}).\n\n"
            f"Agency + dental enrichment may stop until next month or top-up."
        ),
        severity="warning",
    )
    _mark_alert_sent("apollo_credits_low")
    return True


def check_warmup_stale(hours: int = WARMUP_STALE_HOURS) -> bool:
    """Alert if no warmup emails sent in the last N hours."""
    from .warmup import load_schedule

    schedule = load_schedule(create_if_missing=False)
    last_send = schedule.get("last_send")
    if not last_send:
        stale = True
    else:
        try:
            ts = datetime.fromisoformat(str(last_send).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            stale = _utc_now() - ts > timedelta(hours=hours)
        except (TypeError, ValueError):
            stale = True

    emails_today = int(schedule.get("emails_sent_today") or 0)
    if not stale and emails_today > 0:
        return False

    if not stale:
        return False
    if _alert_already_sent("warmup_stale"):
        return False

    send_alert(
        "No warmup emails sent in 24 hours",
        (
            f"No warmup activity detected in the last {hours} hours.\n\n"
            f"last_send: {last_send or 'never'}\n"
            f"emails_sent_today: {emails_today}\n\n"
            f"Run: python -m pipeline.warmup send"
        ),
        severity="warning",
    )
    _mark_alert_sent("warmup_stale")
    return True


def load_send_metrics() -> dict:
    raw = _load_json(SEND_METRICS_PATH, {"sent": 0, "bounced": 0})
    return raw if isinstance(raw, dict) else {"sent": 0, "bounced": 0}


def save_send_metrics(data: dict) -> None:
    _save_json(SEND_METRICS_PATH, data)


def record_send_batch(sent: int, failed: int = 0) -> None:
    """Track sends for bounce-rate monitoring."""
    metrics = load_send_metrics()
    metrics["sent"] = int(metrics.get("sent") or 0) + max(0, sent)
    metrics["failed_smtp"] = int(metrics.get("failed_smtp") or 0) + max(0, failed)
    metrics["last_send_at"] = _utc_now().isoformat(timespec="seconds")
    save_send_metrics(metrics)
    if sent:
        bump_daily_metric("emails_sent", sent)


def record_bounce(count: int = 1) -> None:
    metrics = load_send_metrics()
    metrics["bounced"] = int(metrics.get("bounced") or 0) + count
    save_send_metrics(metrics)


def check_bounce_rate(threshold: float = BOUNCE_RATE_WARN) -> bool:
    """Alert if bounce rate exceeds threshold (bounced / sent)."""
    metrics = load_send_metrics()
    sent = int(metrics.get("sent") or 0)
    bounced = int(metrics.get("bounced") or 0)
    if sent < 20:
        return False
    rate = bounced / sent
    if rate <= threshold:
        return False
    if _alert_already_sent("bounce_rate_high"):
        return False

    send_alert(
        "Bounce rate above 3%",
        (
            f"Bounce rate: {rate:.1%} ({bounced}/{sent} sends).\n"
            f"Threshold: {threshold:.0%}.\n\n"
            f"Review list quality and sending domains."
        ),
        severity="warning",
    )
    _mark_alert_sent("bounce_rate_high")
    return True


def check_deliverability_spam() -> bool:
    """Alert if last deliverability test landed in Spam."""
    from .deliverability import last_test_folder

    folder = (last_test_folder() or "").lower()
    if folder != "spam":
        return False
    if _alert_already_sent("deliverability_spam"):
        return False

    send_alert(
        "Test email went to Spam",
        (
            "The latest deliverability test email was detected in the Spam folder.\n\n"
            "Check domain reputation, warmup volume, and email content."
        ),
        severity="critical",
    )
    _mark_alert_sent("deliverability_spam")
    return True


def run_health_checks_for_job(job_name: str) -> None:
    """Run contextual health checks after a successful job."""
    if job_name in ("research", "sync-leads", "launch_pending", "credit-stats"):
        check_apollo_credits_low()
    if job_name in ("warmup_send", "warmup_process", "warmup_stats"):
        check_warmup_stale()
    if job_name == "send":
        check_bounce_rate()
    if job_name in ("send", "deliverability"):
        check_deliverability_spam()


def build_daily_summary() -> str:
    from .bounce_handler import weekly_bounce_stats

    data = load_daily_metrics()
    today = data.get(_today_key(), {})
    emails = int(today.get("emails_sent") or 0)
    replies = int(today.get("replies") or 0)
    interested = int(today.get("interested") or 0)
    warmup = int(today.get("warmup_emails") or 0)
    credits = apollo_credits_remaining()
    bounce = weekly_bounce_stats()
    bounce_line = (
        f"Bounce rate this week: {bounce['rate'] * 100:.1f}% "
        f"(target: <{bounce['target'] * 100:.0f}%)"
    )
    return (
        f"Today: {emails} emails sent, {replies} replies, {interested} interested. "
        f"Warmup: {warmup} emails.\n\n"
        f"{bounce_line}\n"
        f"Apollo credits remaining: {credits}"
    )


def send_daily_summary(force: bool = False) -> bool:
    """Send daily summary alert (intended for 20:00 UTC cron)."""
    state = load_alert_state()
    today = _today_key()
    if not force and state.get("daily_summary_sent") == today:
        print("daily summary already sent today")
        return False

    body = build_daily_summary()
    ok = send_alert("Daily pipeline summary", body, severity="info")
    if ok or force:
        state["daily_summary_sent"] = today
        save_alert_state(state)
    return ok


def maybe_send_daily_summary() -> None:
    """Send summary once per day when run at 20:00 UTC."""
    if _utc_now().hour == DAILY_SUMMARY_HOUR_UTC:
        send_daily_summary()


def simulate_job_failures(job_name: str = "alert_test", times: int = 3) -> None:
    """Test helper: record N consecutive failures → triggers alert on 3rd."""
    for i in range(times):
        record_job_failure(job_name, f"simulated failure #{i + 1}")


def print_alert_status() -> None:
    """CLI: show failure log and today's metrics."""
    print("=== ALERT STATUS ===")
    print(f"ALERT_EMAIL: {alert_email() or '(not set)'}")
    print(f"ALERT_FROM: {alert_from_email()}")
    print(f"Apollo credits remaining: {apollo_credits_remaining()}")
    print(f"Today's summary: {build_daily_summary()}")
    print("Failure log:")
    print(json.dumps(load_failure_log(), indent=2))
    print("====================")
