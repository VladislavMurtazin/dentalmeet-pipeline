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

# Neighboring areas for geo expansion (primary metro → nearby)
GEO_EXPANSION_MAP: dict[str, list[str]] = {
    "los angeles": ["Orange County, CA", "San Diego, CA", "Inland Empire, CA"],
    "new york": ["New Jersey", "Connecticut", "Long Island, NY"],
    "chicago": ["Illinois", "Indiana", "Wisconsin"],
    "houston": ["Dallas, TX", "Austin, TX", "San Antonio, TX"],
    "miami": ["Fort Lauderdale, FL", "West Palm Beach, FL", "Tampa, FL"],
    "dallas": ["Fort Worth, TX", "Austin, TX", "Houston, TX"],
    "phoenix": ["Scottsdale, AZ", "Tucson, AZ", "Las Vegas, NV"],
    "philadelphia": ["New Jersey", "Delaware", "Baltimore, MD"],
    "san francisco": ["San Jose, CA", "Oakland, CA", "Sacramento, CA"],
    "seattle": ["Portland, OR", "Tacoma, WA", "Spokane, WA"],
    "denver": ["Colorado Springs, CO", "Boulder, CO", "Fort Collins, CO"],
    "atlanta": ["Charlotte, NC", "Nashville, TN", "Birmingham, AL"],
    "boston": ["Rhode Island", "New Hampshire", "Connecticut"],
    "austin": ["San Antonio, TX", "Houston, TX", "Dallas, TX"],
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
    locations = [g.strip() for g in geo.replace(";", ",").split(",") if g.strip()]
    if not locations:
        locations = ["United States"]
    if any(x.lower() in ("nationwide", "usa", "us", "united states") for x in locations):
        return ["United States"]
    return locations


def _normalize_geo_key(geo: str) -> str:
    return " ".join((geo or "").lower().replace(",", " ").split())


def _primary_geo(icp: dict) -> str:
    geo = (icp.get("client_geo") or icp.get("geographic_target") or icp.get("geo") or "").strip()
    if not geo:
        return "United States"
    parts = [g.strip() for g in geo.replace(";", ",").split(",") if g.strip()]
    return parts[0] if parts else geo


def _expand_geo_enabled(icp: dict) -> bool:
    raw = icp.get("expand_geo")
    if isinstance(raw, bool):
        return raw
    text = str(raw or icp.get("notes") or "").lower()
    return "expand_geo" in text or "expand geo" in text


def _suggest_geo_expansion(primary_geo: str) -> list[str]:
    key = _normalize_geo_key(primary_geo)
    for metro, neighbors in GEO_EXPANSION_MAP.items():
        if metro in key:
            return neighbors
    # Generic: append state-level if city, state present
    if "," in primary_geo:
        state = primary_geo.split(",")[-1].strip()
        if state and len(state) <= 3:
            return [state, "United States"]
    return []


def _build_search_keywords(icp: dict) -> list[str]:
    """Combine each DENTAL_SEARCH_VARIANT with client geo."""
    primary = _primary_geo(icp)
    geo_suffix = primary.replace(",", "").strip()
    return [f"{variant} {geo_suffix}".strip() for variant in DENTAL_SEARCH_VARIANTS]


def _save_client_geo_info(
    client_email: str,
    *,
    client_geo: str,
    search_geo: str,
    geo_expanded: bool,
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
    for field, val in (
        ("client_geo", client_geo),
        ("dental_search_geo", search_geo),
        ("geo_expanded", "true" if geo_expanded else "false"),
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


def _api_search_pages(icp: dict, limit: int) -> list[dict]:
    """Search all keyword variants with Apollo filters (verified emails only)."""
    locations = _locations_from_icp(icp)
    keywords_list = _build_search_keywords(icp)
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
                "organization_locations": locations,
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
                people.append(p)
                added += 1
                if len(people) >= limit:
                    break
            print(
                f"Apollo [{keywords[:40]}…]: +{added} "
                f"(total {len(people)}) loc={locations}"
            )
            if len(batch) < per_page:
                break
            page += 1

    return people[:limit]


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

    Plan without bulk_match: reveal emails with people/match per id.
    Respects dental enrichment credit budget.
    """
    from pipeline.credit_budget import can_spend_dental, record_dental_credits

    client_email = (icp.get("client_email") or "").strip().lower()
    primary_geo = _primary_geo(icp)
    icp = dict(icp)
    icp.setdefault("client_geo", primary_geo)
    icp.setdefault("dental_search_geo", primary_geo)

    search_limit = max(limit * 3, 25)
    raw_people = _api_search_pages(icp, limit=search_limit)
    geo_expanded = False

    if len(raw_people) < MIN_GEO_RESULTS:
        suggestions = _suggest_geo_expansion(primary_geo)
        print(
            f"Found {len(raw_people)} practices in {primary_geo}. "
            f"Expand to {', '.join(suggestions[:2]) or 'neighboring areas'}?"
        )
        if _expand_geo_enabled(icp) and suggestions:
            expanded_geo = f"{primary_geo}; " + "; ".join(suggestions[:3])
            icp["dental_search_geo"] = expanded_geo
            icp["geographic_target"] = expanded_geo
            geo_expanded = True
            print(f"Geo expansion enabled → searching {expanded_geo}")
            raw_people = _api_search_pages(icp, limit=search_limit)

    if client_email:
        _save_client_geo_info(
            client_email,
            client_geo=primary_geo,
            search_geo=icp.get("dental_search_geo") or primary_geo,
            geo_expanded=geo_expanded,
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


def build_target_list(client_email: str, limit: int = 500, *, dry_run: bool = False) -> list[dict]:
    """ICP → Apollo (api_search + match) → CSV under targets/."""
    if dry_run:
        try:
            icp = load_client_icp(client_email)
        except (RuntimeError, Exception):
            icp = {
                "client_email": client_email,
                "geo": "Los Angeles, CA",
                "client_geo": "Los Angeles, CA",
                "expand_geo": True,
            }
        keywords = _build_search_keywords(icp)
        primary = _primary_geo(icp)
        print(f"DRY RUN dental search for {client_email}")
        print(f"  client_geo: {primary}")
        print(f"  expand_geo: {_expand_geo_enabled(icp)}")
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

    p_run = sub.add_parser("run", help="Alias for build_list")
    p_run.add_argument("client_email")
    p_run.add_argument("--limit", type=int, default=50)
    p_run.add_argument("--dry-run", action="store_true")

    p_test = sub.add_parser("test", help="Quick find_dental_practices smoke test")
    p_test.add_argument("--geo", default="Texas")
    p_test.add_argument("--limit", type=int, default=5)

    p_csv = sub.add_parser("from_csv", help="Load targets from manual Apollo export CSV")
    p_csv.add_argument("csv_path")

    args = parser.parse_args(argv)
    if args.cmd == "build_list":
        build_target_list(args.client_email, limit=args.limit, dry_run=args.dry_run)
        return 0
    if args.cmd == "run":
        build_target_list(args.client_email, limit=args.limit, dry_run=args.dry_run)
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
