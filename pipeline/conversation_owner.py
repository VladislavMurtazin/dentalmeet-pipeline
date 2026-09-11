"""Remember which SMTP account owns a lead conversation end-to-end.

Once a lead replies into founder@…, every follow-up (presentation pack,
form confirmation, Workspace connect, payment) should keep using that
same From address — not SMTP rotation.
"""

from __future__ import annotations

import json
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


def set_conversation_owner(lead_email: str, owner_email: str) -> str | None:
    """Persist owner for this lead. Returns normalized owner or None."""
    lead = _norm(lead_email)
    owner = _norm(owner_email)
    if not lead or "@" not in lead or not owner or "@" not in owner:
        return None
    data = _load()
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
    """Alias used at send sites."""
    return get_conversation_owner(lead_email)


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
