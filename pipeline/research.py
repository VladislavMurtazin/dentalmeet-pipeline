"""Find agency founders via Apollo API or local CSV fallback.

Daily rotation: keyword by weekday, geography by week number, pagination
tracked in data/research_state.json so each run surfaces NEW leads.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, timedelta
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

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "leads.csv"
STATE_PATH = ROOT / "data" / "research_state.json"
APOLLO_BASE = "https://api.apollo.io/api/v1"

# Monday=0 … Sunday=6
KEYWORDS_BY_WEEKDAY = {
    0: "dental marketing",
    1: "dental SEO",
    2: "dental web design",
    3: "dentist lead generation",
    4: "dental practice marketing",
    5: "orthodontic marketing",
    6: "cosmetic dentist marketing",
}
KEYWORD_VARIANTS = [KEYWORDS_BY_WEEKDAY[i] for i in range(7)]

# Week 1→US, 2→Canada, 3→Australia, 4→UK (then cycle)
REGIONS = [
    ("United States", "US"),
    ("Canada", "Canada"),
    ("Australia", "Australia"),
    ("United Kingdom", "UK"),
]

DEFAULT_STATE = {
    "last_page": 0,
    "last_seen_ids": [],
    "keyword_offset": 0,
    "last_keyword": "",
    "last_region": "",
}

MAX_SEEN_IDS = 500


def get_daily_search_params(day_of_year: int) -> dict:
    """Return Apollo filters for a given day-of-year (1–365/366).

    Keyword rotates by weekday; geography by ISO week number (4-week cycle).
    """
    year = date.today().year
    # Clamp / wrap into a valid date for the current year
    doy = max(1, int(day_of_year))
    try:
        d = date(year, 1, 1) + timedelta(days=doy - 1)
        if d.year != year:
            d = date(year, 12, 31)
    except OverflowError:
        d = date(year, 12, 31)

    weekday = d.weekday()  # Mon=0
    week = d.isocalendar()[1]
    loc_name, loc_code = REGIONS[(week - 1) % len(REGIONS)]

    return {
        "q_keywords": KEYWORDS_BY_WEEKDAY[weekday],
        "organization_locations": [loc_name],
        "region_code": loc_code,
        "weekday": weekday,
        "week": week,
        "person_titles": ["Founder", "CEO", "Owner", "President"],
        "organization_num_employees_ranges": ["1-10", "11-50", "51-200"],
    }


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return dict(DEFAULT_STATE)
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)
    state = dict(DEFAULT_STATE)
    state.update(raw or {})
    if not isinstance(state.get("last_seen_ids"), list):
        state["last_seen_ids"] = []
    try:
        state["last_page"] = int(state.get("last_page") or 0)
    except (TypeError, ValueError):
        state["last_page"] = 0
    try:
        state["keyword_offset"] = int(state.get("keyword_offset") or 0) % len(
            KEYWORD_VARIANTS
        )
    except (TypeError, ValueError):
        state["keyword_offset"] = 0
    return state


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ids = state.get("last_seen_ids") or []
    if len(ids) > MAX_SEEN_IDS:
        state["last_seen_ids"] = ids[-MAX_SEEN_IDS:]
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _existing_sheet_emails() -> set[str]:
    """Emails already in Agencies sheet (empty set if Sheets unavailable)."""
    try:
        from .to_sheets import _existing_emails, get_worksheet

        return _existing_emails(get_worksheet())
    except Exception as e:
        print(f"WARN: could not load sheet emails for dedup: {e}")
        return set()


def _headers() -> dict:
    return {
        "X-Api-Key": os.environ["APOLLO_KEY"],
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _search_apollo(
    page: int,
    q_keywords: str,
    organization_locations: list[str],
    person_titles: list[str] | None = None,
    organization_num_employees_ranges: list[str] | None = None,
    per_page: int = 25,
) -> list[dict]:
    """People API search (no emails on this endpoint)."""
    r = requests.post(
        f"{APOLLO_BASE}/mixed_people/api_search",
        headers=_headers(),
        json={
            "q_keywords": q_keywords,
            "person_titles": person_titles
            or ["Founder", "CEO", "Owner", "President"],
            "organization_locations": organization_locations,
            "organization_num_employees_ranges": organization_num_employees_ranges
            or ["1-10", "11-50", "51-200"],
            "page": page,
            "per_page": per_page,
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
        "verified_email": bool(
            p.get("email_status") == "verified" or p.get("verified_email")
        ),
        "has_email": p.get("has_email"),
        "organization": {
            "name": org.get("name", ""),
            "primary_domain": org.get("primary_domain", ""),
        },
    }


def _enrich_people(found: list[dict]) -> list[dict]:
    """Resolve domains + try email enrichment for a search page."""
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
    for p in people:
        status = (p.get("email_status") or "").lower()
        p["verified_email"] = status == "verified" or bool(p.get("verified_email"))
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


def _filter_new_leads(
    people: list[dict],
    *,
    sheet_emails: set[str],
    seen_ids: set[str],
    require_email: bool,
) -> tuple[list[dict], int]:
    """Keep only leads not already in sheet / last_seen_ids. Returns (new, dupes)."""
    new_people: list[dict] = []
    dupes = 0
    for p in people:
        pid = str(p.get("id") or "").strip()
        email = (p.get("email") or "").strip().lower()

        if pid and pid in seen_ids:
            dupes += 1
            continue
        if email and email in sheet_emails:
            dupes += 1
            if pid:
                seen_ids.add(pid)
            continue
        if require_email and not email:
            continue

        new_people.append(p)
        if pid:
            seen_ids.add(pid)

    return new_people, dupes


def find_agencies(page: int = 1, require_email: bool = True) -> list[dict]:
    """Return NEW people from Apollo (daily rotation) or local CSV.

    Apollo path:
      - loads data/research_state.json
      - uses get_daily_search_params(today)
      - searches page = last_page + 1 (per_page=25)
      - if empty, resets to page 1 and tries the next keyword variant
      - dedupes by sheet email + Apollo person_id in last_seen_ids
      - persists updated state

    ``page`` is kept for CLI compat; Apollo pagination is driven by state
    (explicit page>1 can force a one-off page when state last_page is 0).
    """
    apollo_key = os.environ.get("APOLLO_KEY", "").strip()
    if not apollo_key:
        people = _from_csv()
        if require_email:
            people = [p for p in people if (p.get("email") or "").strip()]
        return people

    state = _load_state()
    doy = date.today().timetuple().tm_yday
    base = get_daily_search_params(doy)
    sheet_emails = _existing_sheet_emails()
    seen_ids = {str(x) for x in (state.get("last_seen_ids") or []) if x}

    keyword_offset = int(state.get("keyword_offset") or 0) % len(KEYWORD_VARIANTS)
    used_keyword = base["q_keywords"]
    used_region = base["region_code"]
    used_page = 1
    found: list[dict] = []

    for attempt in range(len(KEYWORD_VARIANTS)):
        kw_idx = (base["weekday"] + keyword_offset) % len(KEYWORD_VARIANTS)
        keyword = KEYWORD_VARIANTS[kw_idx]
        locations = list(base["organization_locations"])
        region_code = base["region_code"]

        if attempt == 0:
            # Prefer state pagination; allow CLI --page override when > 1
            next_page = int(state.get("last_page") or 0) + 1
            if page and int(page) > 1 and int(state.get("last_page") or 0) == 0:
                next_page = int(page)
        else:
            next_page = 1

        print(
            f"Apollo search attempt {attempt + 1}: "
            f"keyword={keyword!r} region={region_code!r} page={next_page}",
            flush=True,
        )
        found = _search_apollo(
            page=next_page,
            q_keywords=keyword,
            organization_locations=locations,
            person_titles=base["person_titles"],
            organization_num_employees_ranges=base[
                "organization_num_employees_ranges"
            ],
            per_page=25,
        )

        if found:
            used_keyword = keyword
            used_region = region_code
            used_page = next_page
            state["last_page"] = next_page
            state["keyword_offset"] = keyword_offset
            state["last_keyword"] = keyword
            state["last_region"] = region_code
            break

        # Empty page → reset pagination and rotate keyword
        print(f"  empty page; rotating keyword (offset {keyword_offset} → ", end="")
        keyword_offset = (keyword_offset + 1) % len(KEYWORD_VARIANTS)
        print(f"{keyword_offset})", flush=True)
        state["last_page"] = 0
        state["keyword_offset"] = keyword_offset
    else:
        _save_state(state)
        print(
            f"Today's search: keyword={used_keyword!r}, "
            f"region={used_region!r}, page={used_page}"
        )
        print("Found 0 new leads (0 duplicates skipped)")
        return []

    people = _enrich_people(found)
    new_people, dupes = _filter_new_leads(
        people,
        sheet_emails=sheet_emails,
        seen_ids=seen_ids,
        require_email=require_email,
    )

    state["last_seen_ids"] = list(seen_ids)
    _save_state(state)

    print(
        f"Today's search: keyword={used_keyword!r}, "
        f"region={used_region!r}, page={used_page}"
    )
    print(f"Found {len(new_people)} new leads ({dupes} duplicates skipped)")
    return new_people
