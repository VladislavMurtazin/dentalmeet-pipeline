"""Track Apollo credit usage against monthly budgets."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUDGET_PATH = ROOT / "data" / "credit_budget.json"

DEFAULT_LIMITS = {
    # 2600 Apollo credits / month — stretch across agency + dental.
    "agency_search": {"limit": 1300},
    "dental_enrichment": {"limit": 1100, "per_client_limit": 50},
    "reserve": 200,
    "total_available": 2600,
}


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def _default_budget(month: str | None = None) -> dict:
    month = month or current_month()
    return {
        "month": month,
        "agency_search": {"spent": 0, "limit": DEFAULT_LIMITS["agency_search"]["limit"]},
        "dental_enrichment": {
            "spent": 0,
            "limit": DEFAULT_LIMITS["dental_enrichment"]["limit"],
            "per_client_limit": DEFAULT_LIMITS["dental_enrichment"]["per_client_limit"],
            "by_client": {},
        },
        "reserve": DEFAULT_LIMITS["reserve"],
        "total_available": DEFAULT_LIMITS["total_available"],
    }


def load_budget() -> dict:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    month = current_month()
    if not BUDGET_PATH.exists():
        data = _default_budget(month)
        save_budget(data)
        return data
    try:
        data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = _default_budget(month)
        save_budget(data)
        return data
    if data.get("month") != month:
        data = _default_budget(month)
        save_budget(data)
        return data

    # Keep spent; refresh caps when plan changes.
    agency_limit = DEFAULT_LIMITS["agency_search"]["limit"]
    dental_limit = DEFAULT_LIMITS["dental_enrichment"]["limit"]
    per_client = DEFAULT_LIMITS["dental_enrichment"]["per_client_limit"]
    data.setdefault("agency_search", {"spent": 0, "limit": agency_limit})
    data.setdefault(
        "dental_enrichment",
        {
            "spent": 0,
            "limit": dental_limit,
            "per_client_limit": per_client,
            "by_client": {},
        },
    )
    data["agency_search"]["limit"] = agency_limit
    data["dental_enrichment"]["limit"] = dental_limit
    data["dental_enrichment"].setdefault("by_client", {})
    data["dental_enrichment"]["per_client_limit"] = per_client
    data["reserve"] = DEFAULT_LIMITS["reserve"]
    data["total_available"] = DEFAULT_LIMITS["total_available"]
    return data


def save_budget(data: dict) -> None:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def agency_remaining() -> int:
    data = load_budget()
    spent = int(data["agency_search"].get("spent") or 0)
    limit = int(
        data["agency_search"].get("limit")
        or DEFAULT_LIMITS["agency_search"]["limit"]
    )
    return max(0, limit - spent)


def dental_remaining(client_email: str | None = None) -> int:
    data = load_budget()
    dent = data["dental_enrichment"]
    limit = int(dent.get("limit") or DEFAULT_LIMITS["dental_enrichment"]["limit"])
    global_left = max(0, limit - int(dent.get("spent") or 0))
    if not client_email:
        return global_left
    email = client_email.strip().lower()
    per_limit = int(
        dent.get("per_client_limit")
        or DEFAULT_LIMITS["dental_enrichment"]["per_client_limit"]
    )
    client_spent = int((dent.get("by_client") or {}).get(email) or 0)
    return max(0, min(global_left, per_limit - client_spent))


def can_spend_agency(amount: int = 1) -> bool:
    return agency_remaining() >= max(1, int(amount))


def can_spend_dental(amount: int = 1, client_email: str | None = None) -> bool:
    return dental_remaining(client_email) >= max(1, int(amount))


def record_agency_credits(amount: int) -> None:
    amount = max(0, int(amount))
    if amount <= 0:
        return
    data = load_budget()
    data["agency_search"]["spent"] = int(data["agency_search"].get("spent") or 0) + amount
    save_budget(data)


def record_dental_credits(amount: int, client_email: str | None = None) -> None:
    amount = max(0, int(amount))
    if amount <= 0:
        return
    data = load_budget()
    dent = data["dental_enrichment"]
    dent["spent"] = int(dent.get("spent") or 0) + amount
    if client_email:
        email = client_email.strip().lower()
        by_client = dent.setdefault("by_client", {})
        by_client[email] = int(by_client.get(email) or 0) + amount
    save_budget(data)


def print_credit_stats() -> None:
    data = load_budget()
    agency = data["agency_search"]
    dental = data["dental_enrichment"]
    reserve = int(data.get("reserve") or DEFAULT_LIMITS["reserve"])
    total_cap = int(data.get("total_available") or DEFAULT_LIMITS["total_available"])
    agency_spent = int(agency.get("spent") or 0)
    dental_spent = int(dental.get("spent") or 0)
    agency_limit = int(agency.get("limit") or DEFAULT_LIMITS["agency_search"]["limit"])
    dental_limit = int(
        dental.get("limit") or DEFAULT_LIMITS["dental_enrichment"]["limit"]
    )
    total_used = agency_spent + dental_spent
    print("=== CREDIT USAGE ===")
    print(f"Month: {data.get('month', current_month())}")
    print(f"Agency search: {agency_spent}/{agency_limit} spent")
    print(f"Dental enrichment: {dental_spent}/{dental_limit} spent")
    print(f"Reserve: {reserve} available")
    print(f"Total: {total_used}/{total_cap} used")
    print("====================")
