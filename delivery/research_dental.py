"""Find dental practices matching a client's ICP (Apollo api_search, no bulk_match)."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "targets"
APOLLO_BASE = "https://api.apollo.io/api/v1"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", re.I)

DECISION_TITLES = [
    "Owner",
    "Doctor",
    "Founder",
    "CEO",
    "Principal",
    "Dentist",
    "DDS",
    "DMD",
    "Practice Owner",
    "President",
    "Office Manager",
    "Treatment Coordinator",
    "Director",
    "Administrator",
]

DENTAL_SEARCH_VARIANTS = [
    # By specialty (10)
    "cosmetic dentistry",
    "dental implants",
    "orthodontist",
    "pediatric dentist",
    "periodontist",
    "endodontist",
    "oral surgeon",
    "dental veneers",
    "teeth whitening",
    "invisalign provider",
    # By practice type (6)
    "dental clinic",
    "dental practice",
    "dental office",
    "family dentistry",
    "dental group",
    "dental studio",
    # By services (9)
    "emergency dentist",
    "sedation dentistry",
    "full mouth reconstruction",
    "dental crowns",
    "dental bridges",
    "dentures",
    "dental hygiene",
    "dental surgery",
    "preventive dentistry",
]

DENTAL_INDUSTRIES = [
    "Dental",
    "Medical Practices",
    "Healthcare",
    "Hospital & Health Care",
]

MIN_GEO_RESULTS = 50
EXPANSION_PER_STATE_LIMIT = 25

# US state adjacency for geo expansion (all 50 states)
STATE_NEIGHBORS: dict[str, list[str]] = {
    "AL": ["MS", "TN", "GA", "FL"],
    "AK": [],
    "AZ": ["CA", "NV", "UT", "CO", "NM"],
    "AR": ["TX", "OK", "MO", "TN", "MS", "LA"],
    "CA": ["NV", "AZ", "OR"],
    "CO": ["WY", "NE", "KS", "OK", "NM", "AZ", "UT"],
    "CT": ["NY", "MA", "RI"],
    "DE": ["MD", "PA", "NJ"],
    "FL": ["GA", "AL"],
    "GA": ["FL", "AL", "TN", "NC", "SC"],
    "HI": [],
    "ID": ["MT", "WY", "UT", "NV", "OR", "WA"],
    "IL": ["WI", "IN", "KY", "MO", "IA"],
    "IN": ["MI", "OH", "KY", "IL"],
    "IA": ["MN", "WI", "IL", "MO", "NE", "SD"],
    "KS": ["NE", "MO", "OK", "CO"],
    "KY": ["IL", "IN", "OH", "WV", "VA", "TN", "MO"],
    "LA": ["TX", "AR", "MS"],
    "ME": ["NH"],
    "MD": ["PA", "DE", "WV", "VA"],
    "MA": ["NH", "VT", "NY", "CT", "RI"],
    "MI": ["OH", "IN", "WI"],
    "MN": ["WI", "IA", "SD", "ND"],
    "MS": ["LA", "AR", "TN", "AL"],
    "MO": ["IA", "IL", "KY", "TN", "AR", "OK", "KS", "NE"],
    "MT": ["ND", "SD", "WY", "ID"],
    "NE": ["SD", "IA", "MO", "KS", "CO", "WY"],
    "NV": ["OR", "ID", "UT", "AZ", "CA"],
    "NH": ["ME", "MA", "VT"],
    "NJ": ["NY", "PA", "DE"],
    "NM": ["AZ", "UT", "CO", "OK", "TX"],
    "NY": ["VT", "MA", "CT", "NJ", "PA"],
    "NC": ["SC", "GA", "TN", "VA"],
    "ND": ["MT", "SD", "MN"],
    "OH": ["PA", "WV", "KY", "IN", "MI"],
    "OK": ["KS", "MO", "AR", "TX", "NM", "CO"],
    "OR": ["WA", "ID", "NV", "CA"],
    "PA": ["NY", "NJ", "DE", "MD", "WV", "OH"],
    "RI": ["MA", "CT"],
    "SC": ["GA", "NC"],
    "SD": ["ND", "MN", "IA", "NE", "WY", "MT"],
    "TN": ["KY", "VA", "NC", "GA", "AL", "MS", "AR", "MO"],
    "TX": ["NM", "OK", "AR", "LA"],
    "UT": ["ID", "WY", "CO", "NM", "AZ", "NV"],
    "VT": ["NY", "NH", "MA"],
    "VA": ["NC", "TN", "KY", "WV", "MD"],
    "WA": ["ID", "OR"],
    "WV": ["OH", "PA", "MD", "VA", "KY"],
    "WI": ["MI", "MN", "IA", "IL"],
    "WY": ["MT", "SD", "NE", "CO", "UT", "ID"],
}

STATE_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

STATE_NAME_TO_CODE: dict[str, str] = {
    name.lower(): code for code, name in STATE_NAMES.items()
}

# ~100 calls/min ⇒ sleep 0.65s between calls
_MIN_INTERVAL = 0.65
_last_call = 0.0


def _rate_limit() -> None:
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = time.time()


def _apollo_headers() -> dict:
    key = os.environ.get("APOLLO_KEY", "").strip()
    if not key:
        raise RuntimeError("APOLLO_KEY missing")
    return {
        "X-Api-Key": key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _is_valid_email(email: str | None) -> bool:
    if not email or not str(email).strip():
        return False
    e = str(email).strip().lower()
    if e.endswith("@example.com") or e.endswith("@email.com"):
        return False
    return bool(EMAIL_RE.match(e))


def _domain_from_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        host = urlparse(url).netloc or ""
        return host.lower().removeprefix("www.")
    except Exception:
        return ""


def _locations_from_icp(icp: dict) -> list[str]:
    geo = (
        icp.get("dental_search_geo")
        or icp.get("geographic_target")
        or icp.get("geo")
        or "United States"
    ).strip()
    locations = _split_geo_segments(geo)
    if not locations:
        locations = ["United States"]
    if any(x.lower() in ("nationwide", "usa", "us", "united states") for x in locations):
        return ["United States"]
    return locations


def _split_geo_segments(geo: str) -> list[str]:
    """Split multi-location geo on semicolons (preserves 'City, ST' commas)."""
    return [g.strip() for g in (geo or "").split(";") if g.strip()]


def _primary_geo(icp: dict) -> str:
    geo = (icp.get("client_geo") or icp.get("geographic_target") or icp.get("geo") or "").strip()
    if not geo:
        return "United States"
    segments = _split_geo_segments(geo)
    return segments[0] if segments else geo


def _parse_state_code(geo: str) -> str | None:
    """Extract 2-letter US state code from geo string (e.g. 'Los Angeles, CA' → 'CA')."""
    geo = (geo or "").strip()
    if not geo:
        return None
    parts = [p.strip() for p in geo.replace(";", ",").split(",") if p.strip()]
    if len(parts) >= 2:
        candidate = parts[-1].upper()
        if len(candidate) == 2 and candidate in STATE_NEIGHBORS:
            return candidate
    # Whole string might be a state name or code
    lower = geo.lower()
    if lower in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[lower]
    upper = geo.upper()
    if len(upper) == 2 and upper in STATE_NEIGHBORS:
        return upper
    for name, code in STATE_NAME_TO_CODE.items():
        if name in lower:
            return code
    return None


def _neighbor_states(primary_geo: str) -> list[str]:
    """Return neighboring state codes for client's state."""
    code = _parse_state_code(primary_geo)
    if not code:
        return []
    return list(STATE_NEIGHBORS.get(code, []))


def _state_search_label(state_code: str) -> str:
    return STATE_NAMES.get(state_code.upper(), state_code)


def _format_expanded_geo(state_codes: list[str]) -> str:
    return ", ".join(_state_search_label(c) for c in state_codes)


def _expand_geo_enabled(icp: dict) -> bool:
    if icp.get("force_expand_geo"):
        return True
    raw = icp.get("expand_geo")
    if isinstance(raw, bool):
        return raw
    text = str(raw or icp.get("notes") or "").lower()
    return "expand_geo" in text or "expand geo" in text


def _build_search_keywords(icp: dict, *, geo: str | None = None) -> list[str]:
    """Combine each DENTAL_SEARCH_VARIANT with search geo."""
    location = (geo or _primary_geo(icp)).strip()
    geo_suffix = location.replace(",", "").strip()
    return [f"{variant} {geo_suffix}".strip() for variant in DENTAL_SEARCH_VARIANTS]


def _save_client_geo_info(
    client_email: str,
    *,
    client_geo: str,
    search_geo_primary: str,
    search_geo_expanded: str,
    geo_was_expanded: bool,
) -> None:
    from pipeline.to_sheets import get_all_values_safe, update_cell_safe
    from delivery.client_tracker import _clients_ws, _find_matches, ensure_tracker_columns

    ws = _clients_ws()
    cols = ensure_tracker_columns(ws)
    values = get_all_values_safe(ws)
    hits = _find_matches(values, cols, client_email.strip().lower(), "")
    if not hits:
        return
    row_index, _ = hits[-1]
    combined_search = search_geo_primary
    if search_geo_expanded:
        combined_search = f"{search_geo_primary}; {search_geo_expanded}"
    for field, val in (
        ("client_geo", client_geo),
        ("search_geo_primary", search_geo_primary),
        ("search_geo_expanded", search_geo_expanded),
        ("geo_was_expanded", "true" if geo_was_expanded else "false"),
        ("dental_search_geo", combined_search),
        ("geo_expanded", "true" if geo_was_expanded else "false"),
    ):
        if field in cols:
            update_cell_safe(ws, row_index, cols[field], val)


def load_client_icp(client_email: str) -> dict:
    """Read ICP filters from Pilot CRM / Clients."""
    from pipeline.to_sheets import get_all_values_safe, open_sheet_safe, open_worksheet_safe

    email = client_email.strip().lower()
    sh = open_sheet_safe(os.environ.get("SHEET_NAME", "Pilot CRM"))
    ws = open_worksheet_safe(sh, "Clients")
    values = get_all_values_safe(ws)
    if not values:
        raise RuntimeError("Clients sheet empty")
    headers = [h.strip() for h in values[0]]
    col = {h.lower(): i for i, h in enumerate(headers)}

    def get(row, *names, default=""):
        for n in names:
            i = col.get(n.lower())
            if i is not None and i < len(row):
                return (row[i] or "").strip()
        return default

    for row in values[1:]:
        row_email = get(row, "Email", "Workspace Email").lower()
        if row_email == email or get(row, "Workspace Email").lower() == email:
            icp = {
                "client_email": email,
                "company": get(row, "Company"),
                "name": get(row, "Name"),
                "title": get(row, "Title") or "Founder",
                "practice_types": get(row, "ICP"),
                "geographic_target": get(row, "Geo"),
                "client_geo": get(row, "client_geo") or get(row, "Geo"),
                "dental_search_geo": get(row, "dental_search_geo") or get(row, "Geo"),
                "search_geo_primary": get(row, "search_geo_primary") or get(row, "Geo"),
                "search_geo_expanded": get(row, "search_geo_expanded"),
                "geo_was_expanded": get(row, "geo_was_expanded") or get(row, "geo_expanded"),
                "geo_expanded": get(row, "geo_expanded"),
                "expand_geo": get(row, "expand_geo"),
                "services_needed": get(row, "Services"),
                "calendly": get(row, "Calendly"),
                "pricing": get(row, "Pricing"),
                "workspace_status": get(row, "workspace_status") or "pending",
                "notes": get(row, "Notes"),
            }
            print(
                f"ICP loaded for {email}: "
                f"{icp['practice_types'][:60]!r} / {icp['geographic_target']!r}"
            )
            return icp
    raise RuntimeError(f"Client not found in Clients sheet: {email}")


def _normalize_person(raw: dict) -> dict | None:
    """Map Apollo person/contact → our target dict. None if no valid email."""
    org = raw.get("organization") or {}
    email = (raw.get("email") or raw.get("contact_email") or "").strip().lower()
    status = (raw.get("email_status") or raw.get("contact_email_status") or "").lower()

    if not _is_valid_email(email):
        return None
    # Prefer verified; accept likely if API marked has verified elsewhere
    verified = status in ("verified", "likely to engage", "") or bool(
        raw.get("verified") or raw.get("verified_email")
    )
    if status and status not in ("verified", "likely to engage"):
        # skip explicit unverified / unavailable
        if status in ("unavailable", "bounced", "invalid"):
            return None

    domain = (
        (org.get("primary_domain") or "").strip()
        or _domain_from_url(org.get("website_url") or org.get("website") or "")
        or (raw.get("organization_domain") or "")
    )
    last = raw.get("last_name") or ""
    if not last and raw.get("last_name_obfuscated"):
        last = str(raw["last_name_obfuscated"]).replace("*", "")

    return {
        "first_name": raw.get("first_name") or "",
        "last_name": last,
        "email": email,
        "title": raw.get("title") or "",
        "organization_name": org.get("name")
        or raw.get("organization_name")
        or "",
        "organization_domain": domain,
        "city": org.get("city") or raw.get("city") or "",
        "state": org.get("state") or raw.get("state") or "",
        "verified": bool(verified) or status == "verified",
        # aliases used by send_as_client / CSV
        "company": org.get("name") or raw.get("organization_name") or "",
        "domain": domain,
        "verified_email": status == "verified" or bool(raw.get("verified_email")),
        "organization": {
            "name": org.get("name") or "",
            "primary_domain": domain,
            "website_url": org.get("website_url") or "",
            "city": org.get("city") or "",
            "state": org.get("state") or "",
        },
        "id": raw.get("id"),
        "opener": "",
        "signal": "",
        "geo_expanded": bool(raw.get("_geo_expanded") or raw.get("geo_expanded")),
    }


def _match_person_by_id(person_id: str) -> dict | None:
    """Reveal email via people/match (works when bulk_match is locked)."""
    if not person_id:
        return None
    _rate_limit()
    r = requests.post(
        f"{APOLLO_BASE}/people/match",
        headers=_apollo_headers(),
        params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
        json={"id": person_id},
        timeout=45,
    )
    if r.status_code == 403:
        print("WARN: people/match forbidden for this key")
        return None
    if r.status_code != 200:
        print(f"WARN: people/match {r.status_code}: {r.text[:160]}")
        return None
    data = r.json()
    return data.get("person") or data.get("match")


def _api_search_pages(
    icp: dict,
    limit: int,
    *,
    locations: list[str] | None = None,
    keyword_geo: str | None = None,
    geo_expanded: bool = False,
) -> list[dict]:
    """Search keyword variants with Apollo filters (verified emails only)."""
    locs = locations or _locations_from_icp(icp)
    keywords_list = _build_search_keywords(icp, geo=keyword_geo)
    per_variant = max(2, limit // max(len(keywords_list), 1))

    people: list[dict] = []
    seen_ids: set[str] = set()

    for keywords in keywords_list:
        if len(people) >= limit:
            break
        page = 1
        per_page = min(25, per_variant)
        while len(people) < limit:
            _rate_limit()
            payload = {
                "q_keywords": keywords,
                "person_titles": DECISION_TITLES,
                "organization_locations": locs,
                "q_organization_keyword_tags": DENTAL_INDUSTRIES,
                "organization_num_employees_ranges": ["1-10", "11-50", "51-200"],
                "email_status": ["verified"],
                "contact_email_status": ["verified"],
                "page": page,
                "per_page": per_page,
            }
            r = requests.post(
                f"{APOLLO_BASE}/mixed_people/api_search",
                headers=_apollo_headers(),
                json=payload,
                timeout=45,
            )
            if r.status_code != 200:
                print(f"WARN: api_search {r.status_code} for {keywords!r}: {r.text[:120]}")
                break
            batch = r.json().get("people") or []
            if not batch:
                break
            added = 0
            for p in batch:
                pid = str(p.get("id") or "")
                if pid and pid in seen_ids:
                    continue
                if pid:
                    seen_ids.add(pid)
                if geo_expanded:
                    p = dict(p)
                    p["_geo_expanded"] = True
                people.append(p)
                added += 1
                if len(people) >= limit:
                    break
            print(
                f"Apollo [{keywords[:40]}…]: +{added} "
                f"(total {len(people)}) loc={locs}"
            )
            if len(batch) < per_page:
                break
            page += 1

    return people[:limit]


def _expand_to_neighbor_states(
    icp: dict,
    *,
    primary_geo: str,
    existing: list[dict],
    seen_ids: set[str],
) -> tuple[list[dict], list[str], int]:
    """Search neighboring states (25 results each) and merge with primary hits."""
    neighbors = _neighbor_states(primary_geo)
    if not neighbors:
        print(f"No neighboring states mapped for {primary_geo!r}")
        return existing, [], 0

    expanded_people = list(existing)
    used_states: list[str] = []
    before = len(expanded_people)

    for state_code in neighbors:
        state_label = _state_search_label(state_code)
        batch = _api_search_pages(
            icp,
            limit=EXPANSION_PER_STATE_LIMIT,
            locations=[state_label],
            keyword_geo=state_label,
            geo_expanded=True,
        )
        added_here = 0
        for p in batch:
            pid = str(p.get("id") or "")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            expanded_people.append(p)
            added_here += 1
        if added_here:
            used_states.append(state_code)
        print(f"  neighbor {state_code}: +{added_here} practices")

    added_total = len(expanded_people) - before
    return expanded_people, used_states, added_total


def _contacts_search_fallback(icp: dict, limit: int) -> list[dict]:
    """Fallback: contacts/search with first keyword variant."""
    locations = _locations_from_icp(icp)
    keywords = _build_search_keywords(icp)[0] if _build_search_keywords(icp) else "dental practice"
    out: list[dict] = []
    page = 1
    per_page = 25
    while len(out) < limit:
        _rate_limit()
        r = requests.post(
            f"{APOLLO_BASE}/contacts/search",
            headers=_apollo_headers(),
            json={
                "q_keywords": keywords,
                "person_titles": DECISION_TITLES,
                "organization_locations": locations,
                "email_status": ["verified"],
                "contact_email_status": ["verified"],
                "page": page,
                "per_page": per_page,
            },
            timeout=45,
        )
        if r.status_code == 403:
            print("WARN: contacts/search not available on this plan")
            return out
        if r.status_code != 200:
            print(f"WARN: contacts/search {r.status_code}: {r.text[:160]}")
            return out
        batch = r.json().get("contacts") or []
        if not batch:
            break
        out.extend(batch)
        print(f"Apollo contacts/search page {page}: +{len(batch)}")
        if len(batch) < per_page:
            break
        page += 1
    return out[:limit]


def find_dental_practices(icp: dict, limit: int = 500) -> list[dict]:
    """
    Find dental decision-makers via 25 keyword variants + geo.

    Searches primary client geo first. If fewer than MIN_GEO_RESULTS hits,
    expands to neighboring US states (25 results per state).

    Plan without bulk_match: reveal emails with people/match per id.
    Respects dental enrichment credit budget.
    """
    from pipeline.credit_budget import can_spend_dental, record_dental_credits

    client_email = (icp.get("client_email") or "").strip().lower()
    primary_geo = _primary_geo(icp)
    icp = dict(icp)
    icp.setdefault("client_geo", primary_geo)
    icp.setdefault("dental_search_geo", primary_geo)
    icp.setdefault("geographic_target", primary_geo)

    search_limit = max(limit * 3, 25)
    raw_people = _api_search_pages(
        icp,
        limit=search_limit,
        locations=[primary_geo],
        keyword_geo=primary_geo,
    )
    primary_count = len(raw_people)
    geo_was_expanded = False
    search_geo_expanded = ""
    seen_ids = {str(p.get("id") or "") for p in raw_people if p.get("id")}

    should_expand = primary_count < MIN_GEO_RESULTS
    if should_expand:
        city_label = primary_geo.split(",")[0].strip() if "," in primary_geo else primary_geo
        print(
            f"Found only {primary_count} in {city_label or primary_geo}. "
            f"Expanding to neighboring areas."
        )
        raw_people, used_states, added = _expand_to_neighbor_states(
            icp,
            primary_geo=primary_geo,
            existing=raw_people,
            seen_ids=seen_ids,
        )
        if used_states:
            geo_was_expanded = True
            search_geo_expanded = _format_expanded_geo(used_states)
            codes = ", ".join(used_states)
            print(f"Expansion added {added} practices from {codes}")

    if client_email:
        _save_client_geo_info(
            client_email,
            client_geo=primary_geo,
            search_geo_primary=primary_geo,
            search_geo_expanded=search_geo_expanded,
            geo_was_expanded=geo_was_expanded,
        )

    raw_people.sort(key=lambda p: (0 if p.get("has_email") else 1))

    results: list[dict] = []
    skipped_missing = 0
    seen_emails: set[str] = set()

    for p in raw_people:
        if len(results) >= limit:
            break

        normalized = _normalize_person(p)
        if normalized and normalized["email"] not in seen_emails:
            results.append(normalized)
            seen_emails.add(normalized["email"])
            print(
                f"  + {normalized['email']} "
                f"({normalized['organization_name'][:40]})"
            )
            continue

        if not p.get("id"):
            skipped_missing += 1
            continue
        if p.get("has_email") is False:
            skipped_missing += 1
            continue

        if not can_spend_dental(1, client_email or None):
            print("Dental enrichment credit limit reached — stopping match enrichment")
            break

        matched = _match_person_by_id(p["id"])
        if matched:
            if p.get("_geo_expanded"):
                matched["_geo_expanded"] = True
            record_dental_credits(1, client_email or None)
        if not matched:
            skipped_missing += 1
            continue
        normalized = _normalize_person(matched)
        if not normalized or normalized["email"] in seen_emails:
            skipped_missing += 1
            continue
        results.append(normalized)
        seen_emails.add(normalized["email"])
        print(
            f"  + {normalized['email']} "
            f"({normalized['organization_name'][:40]})"
        )

    if len(results) < limit:
        print("Trying contacts/search fallback…")
        for c in _contacts_search_fallback(icp, limit=max(25, limit)):
            if len(results) >= limit:
                break
            if not can_spend_dental(1, client_email or None):
                break
            normalized = _normalize_person(c)
            if not normalized and c.get("id"):
                matched = _match_person_by_id(c["id"])
                if matched:
                    record_dental_credits(1, client_email or None)
                normalized = _normalize_person(matched or {})
            if not normalized or normalized["email"] in seen_emails:
                skipped_missing += 1
                continue
            results.append(normalized)
            seen_emails.add(normalized["email"])

    print(
        f"find_dental_practices: {len(results)} with valid email, "
        f"{skipped_missing} email_missing/skipped "
        f"(from {len(raw_people)} search hits, geo={icp.get('dental_search_geo')})"
    )
    return results[:limit]


def find_dental_practices_from_csv(csv_path: str | Path) -> list[dict]:
    """
    Manual Apollo UI export fallback.

    Expected columns:
      first_name, last_name, email, title, organization_name, organization_domain
    (also accepts company/domain aliases)
    """
    path = Path(csv_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(path)

    out: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # normalize header keys
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            email = row.get("email") or row.get("email address") or ""
            if not _is_valid_email(email):
                continue
            org = row.get("organization_name") or row.get("company") or row.get("organization") or ""
            domain = (
                row.get("organization_domain")
                or row.get("domain")
                or row.get("website")
                or ""
            )
            person = {
                "first_name": row.get("first_name") or row.get("firstname") or "",
                "last_name": row.get("last_name") or row.get("lastname") or "",
                "email": email.lower(),
                "title": row.get("title") or "",
                "organization_name": org,
                "organization_domain": domain,
                "city": row.get("city") or "",
                "state": row.get("state") or "",
                "verified": True,
                "company": org,
                "domain": domain,
                "verified_email": True,
                "organization": {
                    "name": org,
                    "primary_domain": domain,
                    "website_url": "",
                    "city": row.get("city") or "",
                    "state": row.get("state") or "",
                },
                "opener": "",
                "signal": "",
            }
            out.append(person)
    print(f"CSV fallback: {len(out)} contacts from {path}")
    return out


def enrich_practice(practice: dict) -> dict:
    """Scrape site signal for personalization."""
    from pipeline.personalize import fetch_signal, parse_signal

    org = practice.get("organization") or {}
    domain = (
        (practice.get("organization_domain") or practice.get("domain") or "")
        or (org.get("primary_domain") or "")
    ).strip()
    signal = fetch_signal(f"https://{domain}") if domain else ""
    parts = parse_signal(signal)
    practice = dict(practice)
    practice["signal"] = signal
    practice["signal_parts"] = parts
    practice["domain"] = domain
    practice["company"] = (
        practice.get("organization_name")
        or practice.get("company")
        or org.get("name", "")
    )
    practice["opener"] = ""
    fact = parts.get("H1") or parts.get("Page") or parts.get("Meta") or ""
    fact = " ".join(fact.split())
    if fact:
        if len(fact) > 90:
            fact = fact[:87] + "..."
        practice["opener"] = f"Noticed your practice highlights {fact}."
    return practice


def build_target_list(
    client_email: str,
    limit: int = 500,
    *,
    dry_run: bool = False,
    expand_geo: bool = False,
) -> list[dict]:
    """ICP → Apollo (api_search + match) → CSV under targets/."""
    if dry_run:
        try:
            icp = load_client_icp(client_email)
        except (RuntimeError, Exception):
            icp = {
                "client_email": client_email,
                "geo": "Los Angeles, CA",
                "client_geo": "Los Angeles, CA",
            }
        if expand_geo:
            icp["force_expand_geo"] = True
        keywords = _build_search_keywords(icp)
        primary = _primary_geo(icp)
        neighbors = _neighbor_states(primary)
        print(f"DRY RUN dental search for {client_email}")
        print(f"  client_geo: {primary}")
        print(f"  search_geo_primary: {primary}")
        print(f"  expansion threshold: {MIN_GEO_RESULTS} practices")
        if neighbors:
            print(
                f"  neighbor states if < {MIN_GEO_RESULTS}: "
                f"{', '.join(neighbors)} ({_format_expanded_geo(neighbors)})"
            )
            print(f"  per-state limit: {EXPANSION_PER_STATE_LIMIT}")
        print(f"  force_expand_geo: {expand_geo or _expand_geo_enabled(icp)}")
        print(f"  variants: {len(keywords)} keyword combinations")
        print("  sample queries:")
        for q in keywords[:5]:
            print(f"    • {q}")
        print(f"  ... and {max(0, len(keywords) - 5)} more")
        print(f"  Apollo filters: verified email, titles={DECISION_TITLES[:5]}…")
        print(f"  limit: {limit}")
        return []

    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    icp = load_client_icp(client_email)
    if expand_geo:
        icp["force_expand_geo"] = True
    practices = find_dental_practices(icp, limit=limit)
    if not practices:
        safe = client_email.strip().lower().replace("@", "_at_")
        manual = TARGETS_DIR / f"{safe}_manual.csv"
        if manual.exists():
            print(f"Apollo empty — loading manual CSV {manual}")
            practices = find_dental_practices_from_csv(manual)
        else:
            print(
                f"No emails from Apollo. Export from Apollo UI → save as:\n"
                f"  {manual}"
            )

    rows = []
    for i, p in enumerate(practices, start=1):
        try:
            rows.append(enrich_practice(p))
        except Exception as e:
            print(f"enrich fail {i}: {e}")
            rows.append(p)
        if i % 10 == 0:
            print(f"site-enriched {i}/{len(practices)}")

    # Drop any still-invalid emails
    rows = [r for r in rows if _is_valid_email(r.get("email"))]

    safe = client_email.strip().lower().replace("@", "_at_")
    path = TARGETS_DIR / f"{safe}_targets.csv"
    fieldnames = [
        "email",
        "first_name",
        "last_name",
        "title",
        "company",
        "domain",
        "organization_name",
        "organization_domain",
        "city",
        "state",
        "verified_email",
        "geo_expanded",
        "opener",
        "signal",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for p in rows:
            w.writerow(
                {
                    "email": p.get("email", ""),
                    "first_name": p.get("first_name", ""),
                    "last_name": p.get("last_name", ""),
                    "title": p.get("title", ""),
                    "company": p.get("company") or p.get("organization_name", ""),
                    "domain": p.get("domain") or p.get("organization_domain", ""),
                    "organization_name": p.get("organization_name")
                    or p.get("company", ""),
                    "organization_domain": p.get("organization_domain")
                    or p.get("domain", ""),
                    "city": p.get("city", ""),
                    "state": p.get("state", ""),
                    "verified_email": p.get("verified_email", p.get("verified", "")),
                    "geo_expanded": p.get("geo_expanded", ""),
                    "opener": p.get("opener", ""),
                    "signal": p.get("signal", ""),
                }
            )
    print(f"saved {len(rows)} targets → {path}")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dental practice research for a client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build_list", help="Build targets CSV from client ICP")
    p.add_argument("client_email")
    p.add_argument("--limit", type=int, default=50, help="Max practices (default 50)")
    p.add_argument("--dry-run", action="store_true", help="Print search plan only")
    p.add_argument(
        "--expand-geo",
        action="store_true",
        help="Opt in to neighbor-state expansion (auto when < 50 results)",
    )

    p_run = sub.add_parser("run", help="Alias for build_list")
    p_run.add_argument("client_email")
    p_run.add_argument("--limit", type=int, default=50)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--expand-geo", action="store_true")

    p_test = sub.add_parser("test", help="Quick find_dental_practices smoke test")
    p_test.add_argument("--geo", default="Texas")
    p_test.add_argument("--limit", type=int, default=5)

    p_csv = sub.add_parser("from_csv", help="Load targets from manual Apollo export CSV")
    p_csv.add_argument("csv_path")

    args = parser.parse_args(argv)
    if args.cmd == "build_list":
        build_target_list(
            args.client_email,
            limit=args.limit,
            dry_run=args.dry_run,
            expand_geo=args.expand_geo,
        )
        return 0
    if args.cmd == "run":
        build_target_list(
            args.client_email,
            limit=args.limit,
            dry_run=args.dry_run,
            expand_geo=args.expand_geo,
        )
        return 0
    if args.cmd == "test":
        rows = find_dental_practices(
            {"geographic_target": args.geo, "practice_types": "dental"},
            limit=args.limit,
        )
        print(f"\nFirst {min(5, len(rows))} results:")
        for r in rows[:5]:
            print(
                f"  {r['email']} | {r['first_name']} {r['last_name']} | "
                f"{r['title']} @ {r['organization_name']} ({r.get('state')})"
            )
        return 0 if rows else 1
    if args.cmd == "from_csv":
        rows = find_dental_practices_from_csv(args.csv_path)
        for r in rows[:5]:
            print(r["email"], r["organization_name"])
        return 0 if rows else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
