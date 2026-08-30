"""Automatic multi-account deliverability monitoring (inbox vs spam)."""

from __future__ import annotations

import imaplib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "deliverability_log.json"
WEEKLY_PATH = ROOT / "data" / "deliverability_weekly.json"

DEFAULT_WAIT_SECONDS = 300  # 5 minutes

SPAM_RECOMMENDATIONS = """1. Check MX records
2. Verify SPF includes zoho.com
3. Verify DKIM signature
4. Check DMARC policy
5. Consider reducing send volume"""

PROVIDERS = {
    "gmail": {
        "host_env": "DELIVERABILITY_TEST_IMAP_SERVER",
        "default_host": "imap.gmail.com",
        "email_env": "DELIVERABILITY_TEST_EMAIL",
        "password_env": "DELIVERABILITY_TEST_PASSWORD",
        "fallback_email_env": "ALERT_IMAP_EMAIL",
        "fallback_password_env": "ALERT_IMAP_PASSWORD",
        "inbox": ("INBOX",),
        "spam": ('"[Gmail]/Spam"', "[Google Mail]/Spam", "Spam", "Junk"),
    },
    "outlook": {
        "host_env": "DELIVERABILITY_OUTLOOK_IMAP_SERVER",
        "default_host": "outlook.office365.com",
        "email_env": "DELIVERABILITY_OUTLOOK_EMAIL",
        "password_env": "DELIVERABILITY_OUTLOOK_PASSWORD",
        "inbox": ("INBOX",),
        "spam": ("Junk", "Junk Email", "Spam"),
    },
    "yahoo": {
        "host_env": "DELIVERABILITY_YAHOO_IMAP_SERVER",
        "default_host": "imap.mail.yahoo.com",
        "email_env": "DELIVERABILITY_YAHOO_EMAIL",
        "password_env": "DELIVERABILITY_YAHOO_PASSWORD",
        "inbox": ("INBOX",),
        "spam": ("Bulk Mail", "Spam", "Junk"),
    },
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utc_now().date().isoformat()


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


def _env(*keys: str, default: str = "") -> str:
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return default


def provider_credentials(provider: str) -> tuple[str, str, str] | None:
    """Return (host, email, password) or None if not configured."""
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return None
    email_addr = _env(cfg["email_env"], *([cfg["fallback_email_env"]] if cfg.get("fallback_email_env") else []))
    # Also allow ALERT_EMAIL as Gmail test inbox fallback
    if not email_addr and provider == "gmail":
        email_addr = _env("ALERT_EMAIL")
    password = _env(
        cfg["password_env"],
        *([cfg["fallback_password_env"]] if cfg.get("fallback_password_env") else []),
    )
    if not email_addr or not password:
        return None
    host = _env(cfg["host_env"], default=cfg["default_host"])
    return host, email_addr, password.replace(" ", "")


def test_subject(account: str, day: str | None = None) -> str:
    day = day or _today()
    return f"Deliverability test {day} - {account}"


def test_body(account: str) -> str:
    return (
        f"This is an automated deliverability test from {account}.\n"
        f"Please ignore.\n"
    )


def _imap_find_subject(imap: imaplib.IMAP4_SSL, mailbox: str, subject: str) -> bool:
    status, _ = imap.select(mailbox, readonly=True)
    if status != "OK":
        return False
    # SUBJECT search is substring-ish on Gmail; quote carefully
    safe = subject.replace('"', "")
    status, data = imap.search(None, f'(SUBJECT "{safe}")')
    if status != "OK" or not data or not data[0]:
        return False
    return bool(data[0].split())


def check_placement_for_subject(
    provider: str,
    subject: str,
) -> str:
    """Return inbox | spam | not_found for one subject on a provider inbox."""
    creds = provider_credentials(provider)
    if not creds:
        return "not_configured"
    host, email_addr, password = creds
    cfg = PROVIDERS[provider]
    imap = imaplib.IMAP4_SSL(host)
    try:
        imap.login(email_addr, password)
        for box in cfg["spam"]:
            try:
                if _imap_find_subject(imap, box, subject):
                    return "spam"
            except imaplib.IMAP4.error:
                continue
        for box in cfg["inbox"]:
            try:
                if _imap_find_subject(imap, box, subject):
                    return "inbox"
            except imaplib.IMAP4.error:
                continue
        return "not_found"
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def send_tests_from_all_accounts(
    *,
    to_email: str,
    day: str | None = None,
) -> dict[str, bool]:
    """Send one deliverability probe from each SMTP account."""
    from .email_sender import load_smtp_accounts, send_email

    day = day or _today()
    sent: dict[str, bool] = {}
    accounts = load_smtp_accounts()
    if not accounts:
        print("deliverability-test: no SMTP_ACCOUNTS configured")
        return sent

    for account, _pwd in accounts:
        subject = test_subject(account, day)
        body = test_body(account)
        ok = send_email(
            to_email,
            subject,
            body,
            from_email=account,
            skip_unsubscribe=True,
            skip_throttle=True,
        )
        sent[account.lower()] = bool(ok)
        print(f"deliverability send [{account} → {to_email}]: {'OK' if ok else 'FAIL'}")
    return sent


def _alert_spam(account: str, provider: str = "gmail") -> None:
    from .alerts import send_alert

    send_alert(
        f"URGENT: {account} emails going to Spam!",
        (
            f"Deliverability probe for {account} landed in Spam "
            f"on {provider}.\n\n"
            f"URGENT: {account} emails going to Spam! Check SPF/DKIM/DMARC.\n\n"
            f"Recommendations:\n{SPAM_RECOMMENDATIONS}\n"
        ),
        severity="critical",
    )
    try:
        from .send_throttle import record_spam_complaint

        record_spam_complaint(account)
    except Exception as e:
        print(f"spam cooldown hook failed: {e}")


def append_daily_log(entry: dict) -> None:
    data = _load_json(LOG_PATH, {"history": []})
    if not isinstance(data, dict):
        data = {"history": []}
    history = data.setdefault("history", [])
    # Replace same-date entry if re-run
    day = entry.get("date")
    history[:] = [h for h in history if not (isinstance(h, dict) and h.get("date") == day)]
    history.append(entry)
    # Keep ~90 days
    cutoff = (_utc_now().date() - timedelta(days=90)).isoformat()
    history[:] = [
        h for h in history if isinstance(h, dict) and str(h.get("date") or "") >= cutoff
    ]
    history.sort(key=lambda h: str(h.get("date") or ""))
    _save_json(LOG_PATH, data)


def append_weekly_log(entry: dict) -> None:
    data = _load_json(WEEKLY_PATH, {"history": []})
    if not isinstance(data, dict):
        data = {"history": []}
    history = data.setdefault("history", [])
    day = entry.get("date")
    history[:] = [h for h in history if not (isinstance(h, dict) and h.get("date") == day)]
    history.append(entry)
    cutoff = (_utc_now().date() - timedelta(days=365)).isoformat()
    history[:] = [
        h for h in history if isinstance(h, dict) and str(h.get("date") or "") >= cutoff
    ]
    history.sort(key=lambda h: str(h.get("date") or ""))
    _save_json(WEEKLY_PATH, data)


def run_daily_check(
    *,
    wait_seconds: int | None = None,
    skip_send: bool = False,
    day: str | None = None,
) -> dict:
    """Send from each account → wait → IMAP check Gmail inbox/spam."""
    day = day or _today()
    wait_seconds = DEFAULT_WAIT_SECONDS if wait_seconds is None else max(0, int(wait_seconds))

    gmail = provider_credentials("gmail")
    if not gmail:
        raise RuntimeError(
            "Set DELIVERABILITY_TEST_EMAIL + DELIVERABILITY_TEST_PASSWORD "
            "(or ALERT_IMAP_EMAIL + ALERT_IMAP_PASSWORD)"
        )
    _host, to_email, _pwd = gmail

    sent_map: dict[str, bool] = {}
    if not skip_send:
        sent_map = send_tests_from_all_accounts(to_email=to_email, day=day)
    else:
        from .email_sender import load_smtp_accounts

        sent_map = {e.lower(): True for e, _ in load_smtp_accounts()}

    if wait_seconds:
        print(f"Waiting {wait_seconds}s for delivery…")
        time.sleep(wait_seconds)

    results: dict[str, str] = {}
    spam_accounts: list[str] = []
    for account in sent_map:
        if not sent_map.get(account):
            results[account] = "send_failed"
            continue
        subject = test_subject(account, day)
        try:
            folder = check_placement_for_subject("gmail", subject)
        except Exception as e:
            folder = "error"
            print(f"IMAP check failed for {account}: {e}")
        results[account] = folder
        print(f"  {account}: {folder}")
        if folder == "spam":
            spam_accounts.append(account)
            _alert_spam(account, "gmail")

    entry = {
        "date": day,
        "checked_at": _utc_now().isoformat(timespec="seconds"),
        "provider": "gmail",
        "test_inbox": to_email,
        "results": results,
        "spam_accounts": spam_accounts,
    }
    append_daily_log(entry)

    # Keep legacy single-folder state in sync
    try:
        from .deliverability import record_test_result

        if spam_accounts:
            record_test_result("spam")
        elif any(v == "inbox" for v in results.values()):
            record_test_result("inbox")
    except Exception:
        pass

    print("--- deliverability daily ---")
    print(json.dumps({"date": day, "results": results}, indent=2))
    if spam_accounts:
        print(f"SPAM DETECTED: {', '.join(spam_accounts)}")
    else:
        print("No spam placements detected.")
    return entry


def run_weekly_deep_check(
    *,
    wait_seconds: int | None = None,
    day: str | None = None,
) -> dict:
    """Sunday multi-provider check (Gmail + Outlook + Yahoo when configured)."""
    day = day or _today()
    wait_seconds = DEFAULT_WAIT_SECONDS if wait_seconds is None else max(0, int(wait_seconds))

    from .email_sender import load_smtp_accounts

    accounts = [e.lower() for e, _ in load_smtp_accounts()]
    if not accounts:
        raise RuntimeError("No SMTP_ACCOUNTS configured")

    providers_used: list[str] = []
    provider_results: dict[str, dict[str, str]] = {}

    # Send to each configured provider inbox from every account
    for provider in ("gmail", "outlook", "yahoo"):
        creds = provider_credentials(provider)
        if not creds:
            print(f"weekly: skip {provider} (credentials not set)")
            continue
        _host, to_email, _pwd = creds
        providers_used.append(provider)
        print(f"weekly: sending probes to {provider} ({to_email})")
        send_tests_from_all_accounts(to_email=to_email, day=f"{day}-{provider}")

    if not providers_used:
        raise RuntimeError("No deliverability test inboxes configured")

    if wait_seconds:
        print(f"Waiting {wait_seconds}s for multi-provider delivery…")
        time.sleep(wait_seconds)

    spam_hits: list[str] = []
    for provider in providers_used:
        results: dict[str, str] = {}
        for account in accounts:
            subject = test_subject(account, f"{day}-{provider}")
            try:
                folder = check_placement_for_subject(provider, subject)
            except Exception as e:
                folder = "error"
                print(f"weekly IMAP {provider}/{account}: {e}")
            results[account] = folder
            print(f"  [{provider}] {account}: {folder}")
            if folder == "spam":
                spam_hits.append(f"{account}@{provider}")
                _alert_spam(account, provider)
        provider_results[provider] = results

    entry = {
        "date": day,
        "checked_at": _utc_now().isoformat(timespec="seconds"),
        "providers": provider_results,
        "spam_hits": spam_hits,
    }
    append_weekly_log(entry)
    print("--- deliverability weekly ---")
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    return entry


def run_scheduled_check(*, wait_seconds: int | None = None) -> dict:
    """Daily check; on Sundays also run weekly deep check."""
    daily = run_daily_check(wait_seconds=wait_seconds)
    out: dict = {"daily": daily}
    if _utc_now().weekday() == 6:  # Sunday
        print("Sunday — running weekly deep check across providers")
        out["weekly"] = run_weekly_deep_check(wait_seconds=wait_seconds)
    return out


def load_history(days: int = 30) -> list[dict]:
    data = _load_json(LOG_PATH, {"history": []})
    history = data.get("history") if isinstance(data, dict) else []
    if not isinstance(history, list):
        return []
    cutoff = (_utc_now().date() - timedelta(days=max(1, days) - 1)).isoformat()
    return [
        h
        for h in history
        if isinstance(h, dict) and str(h.get("date") or "") >= cutoff
    ]


def deliverability_trend(days: int = 30) -> dict:
    """Summary for analytics dashboard."""
    history = load_history(days)
    series = []
    spam_days = 0
    inbox_ok_days = 0
    for entry in history:
        results = entry.get("results") or {}
        spam = [a for a, f in results.items() if f == "spam"]
        inbox = [a for a, f in results.items() if f == "inbox"]
        if spam:
            spam_days += 1
        if inbox and not spam:
            inbox_ok_days += 1
        series.append(
            {
                "date": entry.get("date"),
                "inbox_count": len(inbox),
                "spam_count": len(spam),
                "spam_accounts": spam,
                "results": results,
                "has_spam": bool(spam),
            }
        )
    return {
        "days": days,
        "entries": len(series),
        "spam_days": spam_days,
        "clean_days": inbox_ok_days,
        "series": series,
        "latest": series[-1] if series else None,
    }


def print_deliverability_report(days: int = 30) -> None:
    trend = deliverability_trend(days)
    print(f"=== DELIVERABILITY REPORT (last {days} days) ===")
    print(f"Checks logged: {trend['entries']}")
    print(f"Days with spam: {trend['spam_days']}")
    print(f"Clean days:     {trend['clean_days']}")
    for row in trend["series"]:
        flag = "SPAM" if row.get("has_spam") else "ok"
        spam = ", ".join(row.get("spam_accounts") or []) or "-"
        print(
            f"  {row.get('date')}: inbox={row.get('inbox_count')} "
            f"spam={row.get('spam_count')} [{flag}] {spam}"
        )
        results = row.get("results") or {}
        for acct, folder in sorted(results.items()):
            mark = " << PROBLEM" if folder == "spam" else ""
            print(f"      {acct}: {folder}{mark}")
    weekly = _load_json(WEEKLY_PATH, {"history": []})
    wh = weekly.get("history") if isinstance(weekly, dict) else []
    if isinstance(wh, list) and wh:
        last = wh[-1]
        print("--- last weekly multi-provider ---")
        print(f"  date: {last.get('date')}")
        for provider, results in (last.get("providers") or {}).items():
            print(f"  {provider}:")
            for acct, folder in sorted((results or {}).items()):
                print(f"    {acct}: {folder}")
    print("===============================================")
