"""Multi-provider SMTP/IMAP account catalog (Zoho + Google Workspace).

Passwords never live in accounts.json — resolve from env, in order:
  1. per-account password_env (e.g. ZOHO_HELLO_PASSWORD)
  2. SMTP_ACCOUNTS + SMTP_APP_PASSWORDS
  3. GOOGLE_SMTP_ACCOUNTS + GOOGLE_SMTP_APP_PASSWORDS
  4. legacy single-account ZOHO_* / GMAIL_* vars
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_PATH = ROOT / "data" / "accounts.json"

DEFAULT_ZOHO = {
    "provider": "zoho",
    "smtp_server": "smtp.zoho.com",
    "smtp_port": 465,
    "imap_server": "imap.zoho.com",
    "imap_port": 993,
    "imap_ssl": True,
    "enabled": True,
    "cold_ready": True,
    "warmup": True,
}

DEFAULT_GOOGLE = {
    "provider": "google",
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "imap_server": "imap.gmail.com",
    "imap_port": 993,
    "imap_ssl": True,
    "enabled": True,
    "cold_ready": False,
    "warmup": True,
}


def _split_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _default_password_env(email: str, provider: str) -> str:
    local = email.split("@", 1)[0].upper().replace(".", "_").replace("-", "_")
    if provider == "google":
        return f"GOOGLE_SMTP_{local}_PASSWORD"
    return f"ZOHO_{local}_PASSWORD"


def _password_map_from_env() -> dict[str, str]:
    """email.lower() → app password from Zoho + Google env lists."""
    out: dict[str, str] = {}

    def _ingest(emails_key: str, passwords_key: str) -> None:
        emails = _split_csv(os.environ.get(emails_key, ""))
        passwords = _split_csv(os.environ.get(passwords_key, ""))
        for i, email in enumerate(emails):
            if i >= len(passwords):
                print(f"WARN: no password for {email} ({emails_key})")
                continue
            out[email.lower()] = passwords[i].replace(" ", "")

    _ingest("SMTP_ACCOUNTS", "SMTP_APP_PASSWORDS")
    _ingest("GOOGLE_SMTP_ACCOUNTS", "GOOGLE_SMTP_APP_PASSWORDS")

    # Legacy single-account fallbacks (also cover ops Gmail inbox)
    for email_key, pwd_key in (
        ("ZOHO_EMAIL", "ZOHO_APP_PASSWORD"),
        ("SMTP_EMAIL", "SMTP_APP_PASSWORD"),
        ("GMAIL_EMAIL", "GMAIL_APP_PASSWORD"),
    ):
        single = os.environ.get(email_key, "").strip()
        pwd = os.environ.get(pwd_key, "").replace(" ", "").strip()
        if single and pwd and single.lower() not in out:
            out[single.lower()] = pwd
    return out


# Named Google env aliases ↔ numbered secrets used in CI (GOOGLE_SMTP_1/2/3).
_GOOGLE_PASSWORD_ALIASES: dict[str, tuple[str, ...]] = {
    "GOOGLE_SMTP_1": ("GOOGLE_SMTP_VLADISLAV_PASSWORD",),
    "GOOGLE_SMTP_VLADISLAV_PASSWORD": ("GOOGLE_SMTP_1",),
    "GOOGLE_SMTP_2": ("GOOGLE_SMTP_FOUNDER_PASSWORD",),
    "GOOGLE_SMTP_FOUNDER_PASSWORD": ("GOOGLE_SMTP_2",),
    "GOOGLE_SMTP_3": ("GOOGLE_SMTP_OUTREACH_PASSWORD",),
    "GOOGLE_SMTP_OUTREACH_PASSWORD": ("GOOGLE_SMTP_3",),
}


def resolve_account_password(acc: dict[str, Any], pwmap: dict[str, str] | None = None) -> str:
    """Resolve app password for an account (password_env first, then CSV map)."""
    env_key = (acc.get("password_env") or "").strip()
    candidates: list[str] = []
    if env_key:
        candidates.append(env_key)
        candidates.extend(_GOOGLE_PASSWORD_ALIASES.get(env_key, ()))
    email = (acc.get("email") or "").strip().lower()
    local = email.split("@", 1)[0].upper().replace(".", "_") if "@" in email else ""
    if local and (acc.get("provider") or "").lower() == "google":
        named = f"GOOGLE_SMTP_{local}_PASSWORD"
        if named not in candidates:
            candidates.append(named)
    for key in candidates:
        pwd = os.environ.get(key, "").replace(" ", "").strip()
        if pwd:
            return pwd
    pwmap = pwmap if pwmap is not None else _password_map_from_env()
    return pwmap.get(email, "")


def default_accounts_catalog() -> dict[str, Any]:
    """Built-in catalog when accounts.json is missing."""
    zoho_emails = _split_csv(os.environ.get("SMTP_ACCOUNTS", ""))
    google_emails = _split_csv(os.environ.get("GOOGLE_SMTP_ACCOUNTS", ""))
    accounts: list[dict] = []
    for email in zoho_emails:
        accounts.append({"email": email, **DEFAULT_ZOHO})
    for email in google_emails:
        accounts.append({"email": email, **DEFAULT_GOOGLE})
    return {
        "primary_domains": ["dentalmeet.agency"],
        "backup_domains": ["meet-agency.com"],
        "accounts": accounts,
    }


def load_accounts_file() -> dict[str, Any]:
    if ACCOUNTS_PATH.exists():
        try:
            raw = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
                raw.setdefault("primary_domains", ["dentalmeet.agency"])
                raw.setdefault("backup_domains", ["meet-agency.com"])
                return raw
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARN: accounts.json unreadable ({e}) — using env defaults")
    return default_accounts_catalog()


def _normalize_account(raw: dict) -> dict[str, Any] | None:
    email = (raw.get("email") or "").strip()
    if not email or "@" not in email:
        return None
    provider = (raw.get("provider") or "").strip().lower()
    domain = email.split("@", 1)[1].lower()
    if not provider:
        provider = "google" if domain.endswith("gmail.com") or "meet-agency" in domain else "zoho"
    base = dict(DEFAULT_GOOGLE if provider == "google" else DEFAULT_ZOHO)
    base.update({k: v for k, v in raw.items() if v is not None})
    base["email"] = email
    base["provider"] = provider
    base["domain"] = domain
    base["smtp_port"] = int(base.get("smtp_port") or 465)
    base["imap_port"] = int(base.get("imap_port") or 993)
    base["imap_ssl"] = bool(base.get("imap_ssl", True))
    base["enabled"] = bool(base.get("enabled", True))
    base["cold_ready"] = bool(base.get("cold_ready", provider != "google"))
    base["warmup"] = bool(base.get("warmup", True))
    if not (base.get("password_env") or "").strip():
        base["password_env"] = _default_password_env(email, provider)
    return base


def load_account_records(
    *,
    enabled_only: bool = True,
    cold_only: bool = False,
    warmup_only: bool = False,
    with_password: bool = True,
) -> list[dict[str, Any]]:
    """Return account dicts; optionally require password from env."""
    catalog = load_accounts_file()
    pwmap = _password_map_from_env() if with_password else {}
    out: list[dict[str, Any]] = []

    for raw in catalog.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        acc = _normalize_account(raw)
        if not acc:
            continue
        if enabled_only and not acc.get("enabled"):
            continue
        if cold_only and not acc.get("cold_ready"):
            continue
        if warmup_only and not acc.get("warmup"):
            continue
        if with_password:
            pwd = resolve_account_password(acc, pwmap)
            if not pwd:
                print(f"WARN: no app password in env for {acc['email']} — skipping")
                continue
            acc["password"] = pwd
        out.append(acc)

    # If catalog empty but env has Zoho list, synthesize
    if not out and with_password:
        for email, pwd in pwmap.items():
            domain = email.split("@", 1)[1] if "@" in email else ""
            provider = "google" if "meet-agency" in domain or domain == "gmail.com" else "zoho"
            base = dict(DEFAULT_GOOGLE if provider == "google" else DEFAULT_ZOHO)
            base.update(
                {
                    "email": email,
                    "password": pwd,
                    "provider": provider,
                    "domain": domain,
                }
            )
            if cold_only and not base.get("cold_ready"):
                continue
            if warmup_only and not base.get("warmup"):
                continue
            out.append(base)

    return out


def load_smtp_accounts(*, cold_only: bool = True) -> list[tuple[str, str]]:
    """Back-compat: [(email, password), ...] for sending."""
    return [
        (a["email"], a["password"])
        for a in load_account_records(cold_only=cold_only, with_password=True)
    ]


def load_warmup_smtp_accounts(*, allow_dry_run_placeholders: bool = False) -> list[tuple[str, str]]:
    """Warmup SMTP pairs from accounts.json; passwords from env (or dry-run placeholders)."""
    catalog = load_account_records(
        warmup_only=True, cold_only=False, with_password=False
    )
    pwmap = _password_map_from_env()
    out: list[tuple[str, str]] = []
    for acc in catalog:
        email = acc["email"]
        pwd = resolve_account_password(acc, pwmap)
        if pwd:
            out.append((email, pwd))
        elif allow_dry_run_placeholders:
            out.append((email, "dry-run-placeholder"))
        else:
            print(f"WARN: no app password in env for {email} — skipping")
    if out:
        return out
    # Legacy: env passwords with no catalog match
    return [
        (a["email"], a["password"])
        for a in load_account_records(warmup_only=True, cold_only=False, with_password=True)
    ]


def own_account_emails() -> set[str]:
    """All configured sending account addresses (lowercase)."""
    return {
        a["email"].lower()
        for a in load_account_records(enabled_only=False, cold_only=False, with_password=False)
    }


def load_reply_imap_accounts(*, with_password: bool = True) -> list[dict[str, Any]]:
    """
    Enabled sending accounts for reply IMAP, plus optional legacy GMAIL_EMAIL inbox.
    """
    accounts = load_account_records(
        enabled_only=True, cold_only=False, with_password=with_password
    )
    seen = {a["email"].lower() for a in accounts}

    # Optional ops Gmail (historical reply landing) — not a cold sender in catalog
    gmail = os.environ.get("GMAIL_EMAIL", "").strip()
    if gmail and gmail.lower() not in seen:
        pwd = ""
        if with_password:
            pwd = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
            if not pwd:
                print(f"WARN: GMAIL_EMAIL set but GMAIL_APP_PASSWORD missing — skipping {gmail}")
                return accounts
        extra = {
            "email": gmail,
            "provider": "google",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 465,
            "imap_server": "imap.gmail.com",
            "imap_port": 993,
            "imap_ssl": True,
            "password_env": "GMAIL_APP_PASSWORD",
            "enabled": True,
            "cold_ready": False,
            "warmup": False,
            "domain": gmail.split("@", 1)[-1].lower(),
            "legacy_ops_inbox": True,
        }
        if with_password:
            extra["password"] = pwd
        accounts.append(extra)
    return accounts


def get_account(email: str) -> dict[str, Any] | None:
    key = (email or "").strip().lower()
    # Metadata first (no password required — avoids WARN spam on diagnostics)
    for acc in load_account_records(enabled_only=False, cold_only=False, with_password=False):
        if acc["email"].lower() == key:
            return acc
    return None


def smtp_endpoint(email: str) -> tuple[str, int, str]:
    """Return (host, port, provider) for an account email."""
    acc = get_account(email)
    if acc:
        return (
            str(acc.get("smtp_server") or "smtp.zoho.com"),
            int(acc.get("smtp_port") or 465),
            str(acc.get("provider") or "zoho"),
        )
    domain = (email or "").split("@")[-1].lower()
    if "meet-agency" in domain or domain == "gmail.com":
        return "smtp.gmail.com", 465, "google"
    return "smtp.zoho.com", 465, "zoho"


def imap_endpoint(email: str) -> tuple[str, int]:
    acc = get_account(email)
    if acc:
        return (
            str(acc.get("imap_server") or "imap.zoho.com"),
            int(acc.get("imap_port") or 993),
        )
    domain = (email or "").split("@")[-1].lower()
    if "meet-agency" in domain or domain == "gmail.com":
        return "imap.gmail.com", 993
    return "imap.zoho.com", 993


def primary_domains() -> list[str]:
    return list(load_accounts_file().get("primary_domains") or ["dentalmeet.agency"])


def backup_domains() -> list[str]:
    return list(load_accounts_file().get("backup_domains") or ["meet-agency.com"])


def domain_of(email: str) -> str:
    return (email or "").split("@")[-1].lower() if "@" in (email or "") else ""


def prefer_google_senders() -> bool:
    """Agency/cold first-touch prefers Google Workspace @meet-agency.com.

    Default on (PREFER_GOOGLE_SENDERS=1). Zoho catalog entries stay in
    accounts.json for warmup / replies / existing threads; new rotation
    must not silently fall back to Zoho when this flag is on.
    """
    return os.environ.get("PREFER_GOOGLE_SENDERS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def prefer_backup_for_cold() -> bool:
    """True when primary-domain cold accounts look unhealthy."""
    from .send_throttle import calculate_health_score, get_account_metrics

    primary = set(primary_domains())
    cold = load_account_records(cold_only=True, with_password=False)
    primary_accs = [a for a in cold if domain_of(a["email"]) in primary]
    if not primary_accs:
        return True
    bad = 0
    for a in primary_accs:
        m = get_account_metrics(a["email"])
        score = calculate_health_score(m)
        if m.in_cooldown or score < 50 or m.spam_rate > 0.3:
            bad += 1
    return bad >= max(1, (len(primary_accs) + 1) // 2)


def filter_cold_accounts_for_send(
    accounts: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], str]:
    """
    Apply domain failover: if primary spammy and backup cold_ready exists,
    prefer backup domain. Returns (accounts, active_domain_label).
    """
    backup = set(backup_domains())
    primary = set(primary_domains())
    by_email = {e.lower(): (e, p) for e, p in accounts}

    cold_meta = {
        a["email"].lower(): a
        for a in load_account_records(cold_only=True, with_password=False)
    }

    if prefer_backup_for_cold():
        backup_pairs = [
            by_email[e]
            for e, meta in cold_meta.items()
            if e in by_email and domain_of(e) in backup and meta.get("cold_ready")
        ]
        if backup_pairs:
            domains = sorted({domain_of(e) for e, _ in backup_pairs})
            label = ",".join(domains)
            print(f"Switching to backup domain {label} due to primary spam/health issues")
            return backup_pairs, label

    primary_pairs = [
        by_email[e]
        for e, meta in cold_meta.items()
        if e in by_email and domain_of(e) in primary
    ]
    if primary_pairs:
        label = ",".join(sorted(primary))
        print(f"Active sending domain: {label}")
        return primary_pairs, label

    # Fall back to whatever cold accounts we have
    label = ",".join(sorted({domain_of(e) for e, _ in accounts})) or "unknown"
    print(f"Active sending domain: {label}")
    return accounts, label
