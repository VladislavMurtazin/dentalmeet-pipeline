"""Find agency founders via Apollo API or local CSV fallback."""

import csv
import os
from pathlib import Path

import requests

# Header aliases (case-insensitive) → Apollo-like field names
HEADER_MAP = {
    "first_name": "first_name",
    "first name": "first_name",
    "last_name": "last_name",
    "last name": "last_name",
    "email": "email",
    "title": "title",
    "organization_name": "organization_name",
    "organization name": "organization_name",
    "organization_primary_domain": "organization_primary_domain",
    "organization primary domain": "organization_primary_domain",
}

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.csv"
APOLLO_BASE = "https://api.apollo.io/api/v1"


def _headers() -> dict:
    return {
        "X-Api-Key": os.environ["APOLLO_KEY"],
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _search_apollo(page: int) -> list[dict]:
    """People API search (no emails on this endpoint)."""
    r = requests.post(
        f"{APOLLO_BASE}/mixed_people/api_search",
        headers=_headers(),
        json={
            "q_keywords": "dental marketing",
            "person_titles": ["Founder", "CEO", "Owner", "President"],
            "organization_locations": ["United States"],
            "organization_num_employees_ranges": ["1-10", "11-50", "51-200"],
            "page": page,
            "per_page": 25,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("people", [])


def _org_domain(name: str) -> str:
    """Resolve company website via organizations/search."""
    if not name:
        return ""
    r = requests.post(
        f"{APOLLO_BASE}/organizations/search",
        headers=_headers(),
        json={"q_organization_name": name, "per_page": 1},
        timeout=30,
    )
    if r.status_code != 200:
        return ""
    orgs = r.json().get("organizations") or []
    if not orgs:
        return ""
    return (orgs[0].get("primary_domain") or "").strip()


def _enrich_batch(people: list[dict]) -> list[dict] | None:
    """Reveal emails via bulk_match. Returns None if key lacks scope (403)."""
    details = [{"id": p["id"]} for p in people if p.get("id")]
    if not details:
        return []
    r = requests.post(
        f"{APOLLO_BASE}/people/bulk_match",
        headers=_headers(),
        params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
        json={"details": details},
        timeout=60,
    )
    if r.status_code == 403:
        return None
    r.raise_for_status()
    matches = r.json().get("matches") or []
    return [m for m in matches if m]


def _attach_domains(people: list[dict]) -> list[dict]:
    """Fill organization.primary_domain when missing (search omits it)."""
    cache: dict[str, str] = {}
    for p in people:
        org = p.setdefault("organization", {}) or {}
        p["organization"] = org
        if org.get("primary_domain"):
            continue
        name = (org.get("name") or "").strip()
        if name not in cache:
            cache[name] = _org_domain(name)
        org["primary_domain"] = cache[name]
    return people


def _normalize_search_person(p: dict) -> dict:
    """Shape api_search person into our common person dict (email may be empty)."""
    org = p.get("organization") or {}
    last = p.get("last_name") or ""
    if not last and p.get("last_name_obfuscated"):
        last = p["last_name_obfuscated"]  # e.g. Mo***n until enrichment
    return {
        "id": p.get("id"),
        "first_name": p.get("first_name", ""),
        "last_name": last,
        "email": p.get("email") or "",
        "title": p.get("title", ""),
        "verified_email": bool(p.get("email_status") == "verified" or p.get("verified_email")),
        "has_email": p.get("has_email"),
        "organization": {
            "name": org.get("name", ""),
            "primary_domain": org.get("primary_domain", ""),
        },
    }


def _from_apollo(page: int) -> list[dict]:
    """Search people, resolve domains, try email enrichment."""
    found = _search_apollo(page)
    found = _attach_domains(found)

    enriched = _enrich_batch(found[:10])  # first chunk probes scope
    if enriched is None:
        print(
            "WARN: Apollo API key cannot access people/bulk_match (emails).\n"
            "      In Apollo → Settings → Integrations → API, create a Master key\n"
            "      (or enable people/match + people/bulk_match), then update APOLLO_KEY.\n"
            "      Until then: export People CSV from Apollo UI → data/leads.csv\n"
            "      Returning search hits without emails (domains filled)."
        )
        return [_normalize_search_person(p) for p in found]

    people = list(enriched)
    for i in range(10, len(found), 10):
        batch = _enrich_batch(found[i : i + 10]) or []
        people.extend(batch)
    # Map Apollo email_status → verified_email for Sheets
    for p in people:
        status = (p.get("email_status") or "").lower()
        p["verified_email"] = status == "verified" or bool(p.get("verified_email"))
    # Re-attach domains (enrichment usually includes org domain, but be safe)
    return _attach_domains(people)


def _normalize_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map raw CSV headers → canonical keys."""
    mapping = {}
    for h in fieldnames or []:
        key = HEADER_MAP.get(h.strip().lower())
        if key:
            mapping[h] = key
    return mapping


def _row_to_person(row: dict, header_map: dict[str, str]) -> dict:
    """Convert a CSV row into Apollo-like person dict."""
    data = {header_map[k]: (v or "").strip() for k, v in row.items() if k in header_map}
    return {
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "email": data.get("email", ""),
        "title": data.get("title", ""),
        "verified_email": True,
        "organization": {
            "name": data.get("organization_name", ""),
            "primary_domain": data.get("organization_primary_domain", ""),
        },
    }


def _from_csv() -> list[dict]:
    """Load people from data/leads.csv (Apollo export or example shape)."""
    if not CSV_PATH.exists():
        raise FileNotFoundError(
            f"No APOLLO_KEY and CSV not found at {CSV_PATH}. "
            "Copy data/leads.csv.example → data/leads.csv"
        )
    people = []
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header_map = _normalize_headers(reader.fieldnames or [])
        for row in reader:
            people.append(_row_to_person(row, header_map))
    return people


def find_agencies(page: int = 1, require_email: bool = True) -> list[dict]:
    """Return people from Apollo or local CSV.

    require_email=True (default) keeps only rows with email — use CSV export
    or an Apollo key with enrichment scopes for a full run.
    """
    apollo_key = os.environ.get("APOLLO_KEY", "").strip()
    people = _from_apollo(page) if apollo_key else _from_csv()
    if require_email:
        people = [p for p in people if (p.get("email") or "").strip()]
    return people
