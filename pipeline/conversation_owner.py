"""Remember which SMTP account owns a lead conversation end-to-end.

Agency cold first-touch prefers Google Workspace (@meet-agency.com)
when PREFER_GOOGLE_SENDERS=1 (default). After the onboarding form,
client-facing mail must stay on Google Workspace so replies are
visible (Zoho IMAP is disabled). Zoho catalog entries remain for
warmup and existing threads.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER_PATH = ROOT / "data" / "conversation_owners.json"
VIA_RE = re.compile(r"via:([^\s;]+)", re.I)


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def strip_plus_alias(addr: str) -> str:
    email = _norm(addr)
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def _lead_keys(email: str) -> list[str]:
    """Exact + plus-stripped keys for lookup."""
    addr = _norm(email)
    if not addr or "@" not in addr:
        return []
    base = strip_plus_alias(addr)
    keys = [addr]
    if base != addr:
        keys.append(base)
    return keys


def _load() -> dict[str, str]:
    if not OWNER_PATH.exists():
        return {}
    try:
        data = json.loads(OWNER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): str(v).lower() for k, v in data.items() if k and v}


def _save(data: dict[str, str]) -> None:
    OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    OWNER_PATH.write_text(
        json.dumps(dict(sorted(data.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def set_conversation_owner(
    lead_email: str, owner_email: str, *, force: bool = False
) -> str | None:
    """Persist owner for this lead. Returns normalized owner or None.

    Once a Google Workspace owner is locked (post-form), refuse a Zoho
    overwrite unless ``force=True``.
    """
    lead = _norm(lead_email)
    owner = _norm(owner_email)
    if not lead or "@" not in lead or not owner or "@" not in owner:
        return None
    data = _load()
    if not force:
        for key in _lead_keys(lead):
            existing = (data.get(key) or "").strip().lower()
            if (
                existing
                and is_google_workspace_sender(existing)
                and not is_google_workspace_sender(owner)
            ):
                print(
                    f"conversation_owner: keeping Google {existing} for {lead} "
                    f"(refusing non-Google {owner})"
                )
                return existing
    for key in _lead_keys(lead):
        data[key] = owner
    _save(data)
    try:
        _write_clients_owner(lead, owner)
    except Exception as e:
        print(f"conversation_owner: Clients write skipped: {e}")
    return owner


def get_conversation_owner(lead_email: str) -> str | None:
    """Resolve owning SMTP account for a lead (json → Clients → Agencies Notes)."""
    lead = _norm(lead_email)
    if not lead:
        return None

    data = _load()
    for key in _lead_keys(lead):
        owner = (data.get(key) or "").strip().lower()
        if owner:
            return owner

    try:
        owner = _read_clients_owner(lead)
        if owner:
            return owner
    except Exception as e:
        print(f"conversation_owner: Clients read skipped: {e}")

    try:
        owner = _read_agencies_via(lead)
        if owner:
            # Cache for next time
            set_conversation_owner(lead, owner)
            return owner
    except Exception as e:
        print(f"conversation_owner: Agencies read skipped: {e}")

    return None


def resolve_from_email(lead_email: str) -> str | None:
    """Alias used at send sites (sticky owner, including pre-form Zoho)."""
    return get_conversation_owner(lead_email)


def is_google_workspace_sender(email: str) -> bool:
    """True for catalog Google / @meet-agency.com sending accounts."""
    addr = _norm(email)
    if not addr or "@" not in addr:
        return False
    try:
        from .smtp_accounts import get_account
    except ImportError:
        return addr.split("@", 1)[1].lower() == "meet-agency.com"

    acc = get_account(addr)
    if not acc:
        return addr.split("@", 1)[1].lower() == "meet-agency.com"
    provider = (acc.get("provider") or "").strip().lower()
    domain = (acc.get("domain") or addr.split("@", 1)[1]).strip().lower()
    if acc.get("legacy_ops_inbox"):
        return False
    return provider == "google" or domain == "meet-agency.com"


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _google_sender_ready(email: str) -> bool:
    """Enabled Google catalog account with an SMTP password in env."""
    addr = _norm(email)
    if not addr or not is_google_workspace_sender(addr):
        return False
    try:
        from .smtp_accounts import get_account, resolve_account_password
    except ImportError:
        return False
    acc = get_account(addr)
    if not acc or not acc.get("enabled", True):
        return False
    if resolve_account_password(acc):
        return True
    # DRY_RUN can lock a catalog Google From without a live app password.
    return _dry_run()


def google_workspace_smtp_accounts() -> list[tuple[str, str]]:
    """[(email, password), ...] enabled Google Workspace senders.

    Live sends require an env password. DRY_RUN may use catalog addresses
    with a placeholder so check_forms can be exercised locally.
    """
    from .smtp_accounts import load_account_records, resolve_account_password

    require_password = not _dry_run()
    records = load_account_records(
        enabled_only=True, cold_only=False, with_password=require_password
    )
    out: list[tuple[str, str]] = []
    for acc in records:
        email = (acc.get("email") or "").strip()
        if not email or acc.get("legacy_ops_inbox"):
            continue
        provider = (acc.get("provider") or "").strip().lower()
        domain = (acc.get("domain") or "").strip().lower()
        if provider != "google" and domain != "meet-agency.com":
            continue
        password = acc.get("password") or resolve_account_password(acc)
        if not password:
            if require_password:
                continue
            password = "dry-run-placeholder"
        out.append((email, password))
    return out


def pick_google_workspace_sender() -> str | None:
    """Health-weighted Google sender via existing throttle rotation."""
    accounts = google_workspace_smtp_accounts()
    if not accounts:
        return None
    from .send_throttle import calculate_health_score, select_next_account

    picked = select_next_account(accounts)
    if picked:
        return _norm(picked[0])
    # Daily cap / cooldown on every Google inbox — still lock a Google owner
    # (never Zoho) so the client thread stays on Workspace.
    best = max(accounts, key=lambda pair: calculate_health_score(pair[0]))
    print(
        f"post-form: Google accounts at capacity/cooldown; "
        f"locking healthiest {best[0]}"
    )
    return _norm(best[0])


def resolve_post_form_from_email(lead_email: str) -> str | None:
    """From address for client mail after the onboarding form.

    Prefers a sticky Google owner already set for this lead; otherwise picks
    a healthy Google Workspace account and persists it. Never falls back to
    Zoho. Returns None when no Google SMTP password is available.
    """
    lead = _norm(lead_email)
    if not lead or "@" not in lead:
        return None

    existing = get_conversation_owner(lead)
    if existing and _google_sender_ready(existing):
        set_conversation_owner(lead, existing)
        print(f"post-form: keeping Google thread owner {existing} for {lead}")
        return existing
    if existing and not is_google_workspace_sender(existing):
        print(
            f"post-form: replacing non-Google owner {existing} with "
            f"Google Workspace for {lead}"
        )
    elif existing:
        print(
            f"post-form: Google owner {existing} has no SMTP password — "
            f"picking another Workspace account for {lead}"
        )

    picked = pick_google_workspace_sender()
    if not picked:
        print(
            "ERROR: post-form client mail requires a Google Workspace "
            "(@meet-agency.com) SMTP password (GOOGLE_SMTP_* / "
            "GOOGLE_SMTP_ACCOUNTS). Refusing Zoho fallback."
        )
        return None
    set_conversation_owner(lead, picked)
    print(f"post-form: set thread owner {picked} for {lead}")
    return picked


def enforce_post_form_from_email(
    lead_email: str, from_email: str | None
) -> str | None:
    """If a Google owner is locked, never send this lead from Zoho."""
    owner = get_conversation_owner(lead_email)
    if owner and is_google_workspace_sender(owner):
        proposed = _norm(from_email)
        if proposed and proposed != owner:
            print(
                f"post-form: keeping sticky Google owner {owner} "
                f"(not {proposed}) for {lead_email}"
            )
        return owner
    return _norm(from_email) or None


def _clients_owner_col(ws) -> int | None:
    """1-based column index for Owner Account (create header if missing)."""
    from .to_sheets import get_all_values_safe, update_cell_safe

    values = get_all_values_safe(ws)
    if not values:
        return None
    headers = [(h or "").strip() for h in values[0]]
    for i, h in enumerate(headers):
        if h.lower() in ("owner account", "owner", "from account", "reply account"):
            return i + 1
    # Append new header
    col = len(headers) + 1
    update_cell_safe(ws, 1, col, "Owner Account")
    return col


def _write_clients_owner(lead_email: str, owner_email: str) -> None:
    from .to_sheets import get_all_values_safe, get_worksheet_safe, update_cell_safe

    try:
        ws = get_worksheet_safe("Clients")
    except Exception:
        return
    values = get_all_values_safe(ws)
    if not values:
        return
    headers = [(h or "").strip() for h in values[0]]
    try:
        email_idx = next(i for i, h in enumerate(headers) if h.lower() == "email")
    except StopIteration:
        return
    owner_col = _clients_owner_col(ws)
    if not owner_col:
        return
    keys = set(_lead_keys(lead_email))
    for r, row in enumerate(values[1:], start=2):
        cell = (row[email_idx] if email_idx < len(row) else "").strip().lower()
        if cell in keys or strip_plus_alias(cell) in keys:
            update_cell_safe(ws, r, owner_col, owner_email)
            return


def _read_clients_owner(lead_email: str) -> str | None:
    from .to_sheets import get_all_values_safe, get_worksheet_safe

    try:
        ws = get_worksheet_safe("Clients")
    except Exception:
        return None
    values = get_all_values_safe(ws)
    if not values:
        return None
    headers = [(h or "").strip() for h in values[0]]
    try:
        email_idx = next(i for i, h in enumerate(headers) if h.lower() == "email")
    except StopIteration:
        return None
    owner_idx = None
    for i, h in enumerate(headers):
        if h.lower() in ("owner account", "owner", "from account", "reply account"):
            owner_idx = i
            break
    if owner_idx is None:
        return None
    keys = set(_lead_keys(lead_email))
    for row in values[1:]:
        cell = (row[email_idx] if email_idx < len(row) else "").strip().lower()
        if cell in keys or strip_plus_alias(cell) in keys:
            owner = (row[owner_idx] if owner_idx < len(row) else "").strip().lower()
            return owner or None
    return None


def _read_agencies_via(lead_email: str) -> str | None:
    """Parse latest via:owner@… marker from Agencies Notes."""
    from .to_sheets import HEADERS, get_all_values_safe, get_worksheet_safe

    try:
        ws = get_worksheet_safe("Agencies")
    except Exception:
        ws = get_worksheet_safe()
    values = get_all_values_safe(ws)
    if not values:
        return None
    headers = [(h or "").strip() for h in values[0]] or list(HEADERS)
    try:
        email_idx = next(i for i, h in enumerate(headers) if h.lower() == "email")
    except StopIteration:
        return None
    try:
        notes_idx = next(i for i, h in enumerate(headers) if h.lower() == "notes")
    except StopIteration:
        return None
    keys = set(_lead_keys(lead_email))
    for row in values[1:]:
        cell = (row[email_idx] if email_idx < len(row) else "").strip().lower()
        if cell not in keys and strip_plus_alias(cell) not in keys:
            continue
        notes = row[notes_idx] if notes_idx < len(row) else ""
        matches = VIA_RE.findall(notes or "")
        if matches:
            return matches[-1].strip().lower().rstrip(".,)")
    return None
