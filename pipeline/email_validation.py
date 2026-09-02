"""Pre-send email validation (Apollo verified trust + ZeroBounce)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "email_validation.json"
CACHE_DAYS = 90

# ZeroBounce statuses we refuse to send to
BLOCK_STATUSES = frozenset(
    {
        "invalid",
        "spamtrap",
        "abuse",
        "do_not_mail",
        "toxic",
    }
)
# Allowed to send
ALLOW_STATUSES = frozenset({"valid", "catch-all", "catch_all", "verified"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {"emails": {}, "stats": {}}
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return {"emails": {}, "stats": {}}
        raw.setdefault("emails", {})
        raw.setdefault("stats", {})
        return raw
    except (OSError, json.JSONDecodeError):
        return {"emails": {}, "stats": {}}


def _save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc_now().isoformat(timespec="seconds")
    CACHE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _api_key() -> str:
    return (os.environ.get("ZEROBOUNCE_API_KEY") or "").strip()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _is_apollo_verified(meta: dict | None) -> bool:
    if not meta:
        return False
    status = str(meta.get("email_status") or meta.get("Email Status") or "").strip().lower()
    if status in ("verified", "valid"):
        return True
    verified = str(meta.get("verified") or meta.get("Verified") or "").strip().lower()
    if verified in ("yes", "true", "1", "manual"):
        return True
    if meta.get("verified_email") is True:
        return True
    lead = meta.get("lead") if isinstance(meta.get("lead"), dict) else None
    if lead:
        return _is_apollo_verified(lead)
    return False


def _cache_fresh(entry: dict) -> bool:
    ts = str(entry.get("checked_at") or "")[:10]
    if not ts:
        return False
    try:
        from datetime import date, timedelta

        d = date.fromisoformat(ts)
        return (_utc_now().date() - d).days <= CACHE_DAYS
    except ValueError:
        return False


def zerobounce_validate(email: str) -> dict[str, Any]:
    """Call ZeroBounce API; return normalized result dict."""
    key = _api_key()
    if not key:
        return {
            "status": "unknown",
            "source": "no_api_key",
            "ok": True,  # fail-open if no key (log warning upstream)
            "raw": {},
        }

    qs = urllib.parse.urlencode({"api_key": key, "email": email})
    url = f"https://api.zerobounce.net/v2/validate?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return {
            "status": "unknown",
            "source": "zerobounce_error",
            "ok": True,  # fail-open on API errors to avoid total outage
            "error": str(e),
            "raw": {},
        }

    status = str(raw.get("status") or "").strip().lower().replace(" ", "_")
    sub = str(raw.get("sub_status") or "").strip().lower()
    if status in BLOCK_STATUSES or sub in BLOCK_STATUSES:
        ok = False
    elif status in ALLOW_STATUSES or status in ("unknown", ""):
        # catch-all / unknown: allow but flag
        ok = status != "invalid"
        if status in ("unknown", ""):
            ok = True
    else:
        ok = status not in BLOCK_STATUSES

    return {
        "status": status or "unknown",
        "sub_status": sub,
        "source": "zerobounce",
        "ok": ok,
        "raw": {
            "status": raw.get("status"),
            "sub_status": raw.get("sub_status"),
            "free_email": raw.get("free_email"),
            "smtp_provider": raw.get("smtp_provider"),
        },
    }


def validate_recipient(
    email: str,
    *,
    meta: dict | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Validate one recipient before send.

    - Apollo verified / Verified=yes|manual → trust (no ZeroBounce call)
    - Else ZeroBounce (if key set); cache results
    """
    addr = _normalize_email(email)
    result: dict[str, Any] = {
        "email": addr,
        "ok": False,
        "status": "missing",
        "source": "",
        "skipped_send": False,
    }
    if not addr or "@" not in addr:
        result["skipped_send"] = True
        result["reason"] = "invalid_format"
        return result

    if _is_apollo_verified(meta):
        result.update(
            {
                "ok": True,
                "status": "verified",
                "source": "apollo_or_sheet",
                "skipped_send": False,
            }
        )
        return result

    cache = _load_cache()
    cached = (cache.get("emails") or {}).get(addr)
    if not force and isinstance(cached, dict) and _cache_fresh(cached):
        ok = bool(cached.get("ok"))
        result.update(
            {
                "ok": ok,
                "status": cached.get("status") or "unknown",
                "source": "cache",
                "skipped_send": not ok,
                "reason": cached.get("reason"),
            }
        )
        return result

    if not _api_key():
        print(
            f"WARN: ZEROBOUNCE_API_KEY not set — allowing {addr} without "
            f"third-party validation (not Apollo-verified)"
        )
        result.update(
            {
                "ok": True,
                "status": "unvalidated",
                "source": "no_api_key",
                "skipped_send": False,
            }
        )
        return result

    zb = zerobounce_validate(addr)
    ok = bool(zb.get("ok"))
    entry = {
        "ok": ok,
        "status": zb.get("status"),
        "sub_status": zb.get("sub_status"),
        "source": zb.get("source"),
        "checked_at": _utc_now().isoformat(timespec="seconds"),
        "reason": None if ok else f"zerobounce:{zb.get('status')}",
    }
    cache.setdefault("emails", {})[addr] = entry
    stats = cache.setdefault("stats", {})
    stats["checked"] = int(stats.get("checked") or 0) + 1
    if ok:
        stats["valid"] = int(stats.get("valid") or 0) + 1
    else:
        stats["invalid"] = int(stats.get("invalid") or 0) + 1
    _save_cache(cache)

    result.update(
        {
            "ok": ok,
            "status": entry["status"],
            "source": "zerobounce",
            "skipped_send": not ok,
            "reason": entry.get("reason"),
        }
    )
    return result


def validate_batch(recipients: list[dict]) -> dict[str, Any]:
    """Validate a send batch; return summary + filterable skip list."""
    checked = 0
    valid = 0
    invalid = 0
    skipped: list[dict] = []
    allowed: list[dict] = []

    for item in recipients:
        to = (item.get("to") or "").strip()
        meta = {
            "verified": item.get("verified") or item.get("Verified"),
            "email_status": item.get("email_status"),
            "lead": item.get("lead"),
        }
        # Pull Verified from lead row if present
        lead = item.get("lead")
        if isinstance(lead, dict):
            meta["Verified"] = lead.get("Verified")
            meta["verified_email"] = lead.get("verified_email")
            meta["email_status"] = lead.get("email_status") or meta.get("email_status")

        res = validate_recipient(to, meta=meta)
        if res.get("source") == "zerobounce":
            checked += 1
            if res.get("ok"):
                valid += 1
            else:
                invalid += 1
        if res.get("ok"):
            allowed.append(item)
        else:
            skipped.append({**item, "validation": res})
            print(
                f"validation skip {to}: status={res.get('status')} "
                f"source={res.get('source')}"
            )

    total_zb = checked
    invalid_pct = round(100.0 * invalid / total_zb, 1) if total_zb else 0.0
    summary = {
        "checked": checked,
        "valid": valid,
        "invalid": invalid,
        "invalid_pct": invalid_pct,
        "allowed": len(allowed),
        "skipped": len(skipped),
    }
    print(
        f"Validation: {checked} checked, {valid} valid, {invalid} invalid "
        f"({invalid_pct}% invalid)"
    )
    if total_zb >= 10 and invalid_pct > 5.0:
        try:
            from .alerts import send_alert

            send_alert(
                f"High invalid email rate: {invalid_pct}%",
                (
                    f"ZeroBounce batch: checked={checked}, valid={valid}, "
                    f"invalid={invalid} ({invalid_pct}%).\n"
                    f"Review lead sources; high invalid rates hurt sender reputation."
                ),
                severity="warning",
            )
        except Exception as e:
            print(f"validation alert failed: {e}")

    return {"summary": summary, "allowed": allowed, "skipped": skipped}


def is_send_paused() -> tuple[bool, str]:
    """Global outbound pause flag (blacklist / manual)."""
    path = ROOT / "data" / "send_pause.json"
    if not path.exists():
        return False, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ""
    if not data.get("paused"):
        return False, ""
    return True, str(data.get("reason") or "send paused")


def set_send_pause(paused: bool, reason: str = "") -> None:
    path = ROOT / "data" / "send_pause.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "paused": bool(paused),
                "reason": reason,
                "updated_at": _utc_now().isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
