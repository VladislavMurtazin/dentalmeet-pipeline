"""Weekly DNS blacklist checks for domain (RHSBL) + optional IP lists.

Zoho outbound uses shared IPs — IP DNSBLs are noisy/slow, so default is
domain-only (Spamhaus DBL / SURBL). Set BLACKLIST_CHECK_IPS=1 to also
probe a few smtp/mx A records.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "blacklist_log.json"
DEFAULT_DOMAINS = ("dentalmeet.agency", "meet-agency.com")
DNS_TIMEOUT_SEC = 2.0

DNSBLS = [
    ("Spamhaus ZEN", "zen.spamhaus.org"),
    ("SpamCop", "bl.spamcop.net"),
    ("Barracuda", "b.barracudacentral.org"),
]

DOMAIN_BLS = [
    ("Spamhaus DBL", "dbl.spamhaus.org"),
    ("SURBL multi", "multi.surbl.org"),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_log() -> dict:
    if not LOG_PATH.exists():
        return {"checks": []}
    try:
        raw = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"checks": []}
        raw.setdefault("checks", [])
        return raw
    except (OSError, json.JSONDecodeError):
        return {"checks": []}


def _save_log(data: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc_now().isoformat(timespec="seconds")
    LOG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _dns_lookup(hostname: str) -> bool | None:
    """True=listed, False=not listed, None=timeout/error."""
    box: dict[str, bool | None] = {"result": None}

    def _worker() -> None:
        try:
            socket.gethostbyname(hostname)
            box["result"] = True
        except OSError:
            box["result"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=DNS_TIMEOUT_SEC)
    if t.is_alive():
        return None
    return box["result"]


def resolve_smtp_ips() -> list[str]:
    ips: list[str] = []
    box: dict[str, list[str]] = {"ips": []}

    def _worker() -> None:
        try:
            for item in socket.getaddrinfo("smtp.zoho.com", 465, type=socket.SOCK_STREAM):
                ip = item[4][0]
                if ":" not in ip and ip not in box["ips"]:
                    box["ips"].append(ip)
        except OSError:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=DNS_TIMEOUT_SEC)
    for ip in box["ips"][:2]:
        ips.append(ip)
    return ips


def _reverse_ip(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) != 4:
        return ""
    return ".".join(reversed(parts))


def domains_to_check() -> list[str]:
    """Primary + backup from accounts.json, with safe defaults."""
    try:
        from .smtp_accounts import backup_domains, primary_domains

        out: list[str] = []
        for d in list(primary_domains()) + list(backup_domains()):
            d = (d or "").strip().lower()
            if d and d not in out:
                out.append(d)
        if out:
            return out
    except Exception:
        pass
    return list(DEFAULT_DOMAINS)


def run_blacklist_check(*, domain: str | None = None, pause_on_hit: bool = True) -> dict:
    """Check one domain, or all configured domains when domain is None."""
    if domain:
        return _check_one_domain(domain.strip().lower(), pause_on_hit=pause_on_hit)

    results = []
    any_listed = False
    for d in domains_to_check():
        entry = _check_one_domain(d, pause_on_hit=pause_on_hit)
        results.append(entry)
        if entry.get("listed"):
            any_listed = True
    return {
        "checked_at": _utc_now().isoformat(timespec="seconds"),
        "domains": [r.get("domain") for r in results],
        "results": results,
        "listed": any_listed,
    }


def _check_one_domain(domain: str, *, pause_on_hit: bool = True) -> dict:
    hits: list[dict] = []
    timeouts = 0

    for name, zone in DOMAIN_BLS:
        listed = _dns_lookup(f"{domain}.{zone}")
        if listed is None:
            timeouts += 1
            print(f"blacklist timeout: {name} ({domain})")
            continue
        if listed:
            hits.append({"type": "domain", "target": domain, "list": name, "zone": zone})
            print(f"LISTED domain {domain} on {name}")

    ips: list[str] = []
    check_ips = os.environ.get("BLACKLIST_CHECK_IPS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if check_ips:
        ips = resolve_smtp_ips()
        for ip in ips:
            rev = _reverse_ip(ip)
            if not rev:
                continue
            for name, zone in DNSBLS:
                listed = _dns_lookup(f"{rev}.{zone}")
                if listed is None:
                    timeouts += 1
                    continue
                if listed:
                    hits.append({"type": "ip", "target": ip, "list": name, "zone": zone})
                    print(f"LISTED IP {ip} on {name}")

    entry = {
        "checked_at": _utc_now().isoformat(timespec="seconds"),
        "domain": domain,
        "ips_checked": ips,
        "hits": hits,
        "listed": bool(hits),
        "timeouts": timeouts,
    }

    log = _load_log()
    log["checks"] = (log.get("checks") or [])[-50:] + [entry]
    _save_log(log)

    if hits:
        lists = ", ".join(sorted({h["list"] for h in hits}))
        body = (
            f"BLACKLIST ALERT for {domain}\n\n"
            f"Lists: {lists}\n"
            f"Details:\n"
            + "\n".join(f"- {h['type']} {h['target']} → {h['list']}" for h in hits)
            + "\n\nOutbound paused until you clear: python -m pipeline.main send-pause off"
        )
        try:
            from .alerts import send_alert

            send_alert(f"BLACKLIST ALERT: {domain} in {lists}", body, severity="critical")
        except Exception as e:
            print(f"blacklist alert failed: {e}")

        if pause_on_hit:
            try:
                from .email_validation import set_send_pause

                set_send_pause(True, f"blacklist: {domain} → {lists}")
                print("send_pause.json → paused=true")
            except Exception as e:
                print(f"send pause failed: {e}")
    else:
        print(
            f"blacklist ok: {domain} not listed "
            f"(ips_checked={len(ips)}, timeouts={timeouts})"
        )

    return entry


def print_recent(n: int = 5) -> None:
    log = _load_log()
    checks = (log.get("checks") or [])[-n:]
    if not checks:
        print("No blacklist checks logged yet.")
        return
    for c in checks:
        flag = "LISTED" if c.get("listed") else "clean"
        print(f"{c.get('checked_at')}: {flag} hits={len(c.get('hits') or [])}")
