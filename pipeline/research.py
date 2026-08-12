"""Find agency founders via Apollo API or local CSV fallback.

Daily run: 5 broad US keyword variants (up to 3 pages × 100 each),
optional international fallback if US yield is low, then dedupe and
return only NEW leads vs Agencies sheet / research_state.json.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
logger.propagate = False
logger.addHandler(logging.NullHandler())

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

PERSON_TITLES = [
    "Founder",
    "CEO",
    "Owner",
    "President",
    "Director",
    "Co-Founder",
    "Managing Director",
]

# Variant 5: healthcare marketing + dental-leaning titles
DENTAL_TITLES = PERSON_TITLES + [
    "Dental Marketing Director",
    "Dental Marketing Manager",
    "VP of Marketing",
    "Head of Marketing",
]

EMPLOYEE_RANGES = ["1-10", "11-50", "51-200"]
US_LOCATION = ["United States"]
INTL_LOCATIONS = ["United Kingdom", "Canada", "Australia"]

PER_PAGE = 100
MAX_PAGES = 3
US_MIN_HITS_BEFORE_INTL = 20
MAX_SEEN_IDS = 5000
NEW_LEADS_CAP = 100  # max NEW leads returned / added per research run


def get_daily_search_variants(day_of_year: int | None = None) -> list[dict]:
    """Return the 5 broad US search variants (day_of_year unused — fixed set).

    Each item has q_keywords, organization_locations, person_titles, region label.
    """
    _ = day_of_year  # kept for call-site compat
    base = {
        "organization_locations": list(US_LOCATION),
        "person_titles": list(PERSON_TITLES),
        "email_status": ["verified"],
        "region": "US",
    }
    return [
        {**base, "q_keywords": "dental marketing"},
        {**base, "q_keywords": "dental SEO"},
        {**base, "q_keywords": "dentist marketing"},
        {**base, "q_keywords": "dental web design"},
        {
            **base,
            "q_keywords": "healthcare marketing",
            "person_titles": list(DENTAL_TITLES),
        },
    ]


def get_international_variant() -> dict:
    """Optional 6th variant when US yield is low."""
    return {
        "q_keywords": "dental marketing agency",
        "organization_locations": list(INTL_LOCATIONS),
        "person_titles": list(PERSON_TITLES),
        "email_status": ["verified"],
        "region": "UK/CA/AU",
    }


def get_daily_search_params(day_of_year: int | None = None) -> dict:
    """Back-compat: first US variant expanded."""
    v = get_daily_search_variants(day_of_year)[0]
    return {
        "q_keywords": v["q_keywords"],
        "organization_locations": list(v["organization_locations"]),
        "region_code": "US",
        "person_titles": list(v["person_titles"]),
        "email_status": list(v.get("email_status") or ["verified"]),
        "organization_num_employees_ranges": list(EMPLOYEE_RANGES),
    }


def _variant_label(variant: dict) -> str:
    kw = (variant.get("q_keywords") or "").strip()
    region = (variant.get("region") or "US").strip()
    return f"{kw}, {region}"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_seen_ids": []}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_seen_ids": []}
    if not isinstance(raw, dict):
        return {"last_seen_ids": []}
    if not isinstance(raw.get("last_seen_ids"), list):
        raw["last_seen_ids"] = []
    return raw


def _save_run_state(
    *,
    variants_run: int,
    total_hits: int,
    new_added: int,
    duplicates_skipped: int,
    last_seen_ids: list[str],
) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ids = last_seen_ids[-MAX_SEEN_IDS:]
    payload = {
        "date": date.today().isoformat(),
        "variants_run": variants_run,
        "total_hits": total_hits,
        "new_added": new_added,
        "duplicates_skipped": duplicates_skipped,
        "last_seen_ids": ids,
    }
    STATE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
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
    *,
    page: int,
    q_keywords: str,
    organization_locations: list[str],
    person_titles: list[str] | None = None,
    email_status: list[str] | None = None,
    organization_num_employees_ranges: list[str] | None = None,
    per_page: int = PER_PAGE,
) -> list[dict]:
    """People API search (mixed_people/api_search)."""
    payload: dict = {
        "q_keywords": q_keywords,
        "person_titles": person_titles or list(PERSON_TITLES),
        "organization_locations": organization_locations,
        "organization_num_employees_ranges": organization_num_employees_ranges
        or list(EMPLOYEE_RANGES),
        "page": page,
        "per_page": per_page,
    }
    if email_status:
        payload["contact_email_status"] = email_status
        payload["email_status"] = email_status

    r = requests.post(
        f"{APOLLO_BASE}/mixed_people/api_search",
        headers=_headers(),
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("people", []) or []


def _search_variant_pages(variant: dict) -> tuple[list[dict], list[tuple[int, int]]]:
    """Fetch up to MAX_PAGES for a variant. Returns (people, [(page, count), ...])."""
    all_people: list[dict] = []
    seen_ids: set[str] = set()
    page_counts: list[tuple[int, int]] = []

    for page in range(1, MAX_PAGES + 1):
        try:
            batch = _search_apollo(
                page=page,
                q_keywords=variant["q_keywords"],
                organization_locations=list(variant["organization_locations"]),
                person_titles=list(variant.get("person_titles") or PERSON_TITLES),
                email_status=list(variant.get("email_status") or ["verified"]),
                per_page=PER_PAGE,
            )
        except Exception as e:
            print(
                f"  WARN page {page} failed: {e}",
                flush=True,
            )
            page_counts.append((page, 0))
            break

        page_counts.append((page, len(batch)))
        if not batch:
            break

        for p in batch:
            pid = str(p.get("id") or "").strip()
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            all_people.append(p)

        if len(batch) < PER_PAGE:
            # Last page of results
            break
        time.sleep(0.35)

    return all_people, page_counts


def _format_page_summary(page_counts: list[tuple[int, int]]) -> str:
    if not page_counts:
        return "0 hits"
    counts = list(page_counts)
    # Drop trailing empty page used only as stop signal
    if len(counts) > 1 and counts[-1][1] == 0:
        counts = counts[:-1]
    parts = [f"page {p}={n}" for p, n in counts]
    total = sum(n for _, n in counts)
    return f"{', '.join(parts)} → {total} hits"


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

    def _post() -> requests.Response:
        return requests.post(
            f"{APOLLO_BASE}/people/bulk_match",
            headers=_headers(),
            params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
            json={"details": details},
            timeout=60,
        )

    try:
        r = _post()
        if r.status_code == 403:
            return None
        r.raise_for_status()
        data = r.json()
        matches = data.get("matches") or []
        return [m for m in matches if m]
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status == 422:
            logger.warning(
                "Apollo bulk_match returned 422 for batch of %s people, "
                "skipping this batch",
                len(people),
            )
            print(
                f"WARN: Apollo bulk_match returned 422 for batch of "
                f"{len(people)} people, skipping this batch",
                flush=True,
            )
            return []
        if status == 429:
            logger.warning(
                "Apollo rate limit hit, sleeping 60s and retrying once"
            )
            print(
                "WARN: Apollo rate limit hit, sleeping 60s and retrying once",
                flush=True,
            )
            time.sleep(60)
            try:
                r2 = _post()
                if r2.status_code == 403:
                    return None
                r2.raise_for_status()
                matches = r2.json().get("matches") or []
                return [m for m in matches if m]
            except Exception as retry_err:
                logger.warning(
                    "bulk_match failed after 429 retry: %s, skipping batch",
                    retry_err,
                )
                print(
                    f"WARN: bulk_match failed after 429 retry: {retry_err}, "
                    "skipping batch",
                    flush=True,
                )
                return []
        if status >= 500:
            logger.warning(
                "Apollo server error %s, skipping batch",
                status,
            )
            print(
                f"WARN: Apollo server error {status}, skipping batch",
                flush=True,
            )
            return []
        raise
    except Exception as e:
        logger.warning("bulk_match failed: %s, skipping batch", e)
        print(f"WARN: bulk_match failed: {e}, skipping batch", flush=True)
        return []


def _person_enrichable(p: dict) -> bool:
    """True if person has enough fields for a safe bulk_match attempt."""
    if not p.get("id"):
        return False
    first = (p.get("first_name") or "").strip()
    last = (p.get("last_name") or p.get("last_name_obfuscated") or "").strip()
    if not first and not last:
        return False
    email = (p.get("email") or "").strip()
    linkedin = str(p.get("linkedin_url") or p.get("linkedin") or "").strip()
    # Apollo api_search often omits cleartext email/linkedin but sets has_email;
    # bulk_match can still reveal via id in that case.
    has_email_flag = bool(p.get("has_email"))
    if not email and not linkedin and not has_email_flag:
        return False
    return True


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
        last = p["last_name_obfuscated"]
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
    """Try email enrichment (domains filled later for NEW leads only)."""
    if not found:
        return []

    enrichable: list[dict] = []
    skipped = 0
    for p in found:
        if _person_enrichable(p):
            enrichable.append(p)
        else:
            skipped += 1
    if skipped:
        msg = f"Skipped {skipped} people with insufficient data for enrichment"
        logger.warning(msg)
        print(msg, flush=True)

    if not enrichable:
        return [_normalize_search_person(p) for p in found]

    enriched = _enrich_batch(enrichable[:10])
    if enriched is None:
        print(
            "WARN: Apollo API key cannot access people/bulk_match (emails).\n"
            "      Returning search hits without emails."
        )
        return [_normalize_search_person(p) for p in found]

    people = list(enriched)
    for i in range(10, len(enrichable), 10):
        batch = _enrich_batch(enrichable[i : i + 10]) or []
        people.extend(batch)
        time.sleep(0.2)
    for p in people:
        status = (p.get("email_status") or "").lower()
        p["verified_email"] = status == "verified" or bool(p.get("verified_email"))
        # Ensure organization dict exists
        if not isinstance(p.get("organization"), dict):
            p["organization"] = p.get("organization") or {}
    return people


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
    run_emails: set[str],
    require_email: bool,
) -> tuple[list[dict], int]:
    """Keep only leads not already in sheet / last_seen_ids / this run.

    Returns (new_people, duplicates_skipped).
    """
    new_people: list[dict] = []
    dupes = 0
    for p in people:
        pid = str(p.get("id") or "").strip()
        email = (p.get("email") or "").strip().lower()

        if pid and pid in seen_ids:
            dupes += 1
            continue
        if email and (email in sheet_emails or email in run_emails):
            dupes += 1
            if pid:
                seen_ids.add(pid)
            continue
        if require_email and not email:
            continue

        new_people.append(p)
        if pid:
            seen_ids.add(pid)
        if email:
            run_emails.add(email)
            sheet_emails.add(email)

    return new_people, dupes


def _run_one_variant(
    index: int,
    variant: dict,
    *,
    sheet_emails: set[str],
    seen_ids: set[str],
    run_emails: set[str],
    require_email: bool,
) -> tuple[list[dict], int, int]:
    """Search+enrich one variant. Returns (new_people, hits, dupes_in_variant)."""
    label = _variant_label(variant)
    found, page_counts = _search_variant_pages(variant)
    hits = len(found)
    pages_txt = _format_page_summary(page_counts)
    print(f"Variant {index} ({label}): {pages_txt}", flush=True)

    if not found:
        return [], 0, 0

    keyword = (variant.get("q_keywords") or label or "?").strip()
    try:
        people = _enrich_people(found)
    except Exception as e:
        logger.error(
            "Variant '%s' enrichment failed: %s, continuing",
            keyword,
            e,
        )
        print(
            f"ERROR: Variant '{keyword}' enrichment failed: {e}, continuing",
            flush=True,
        )
        people = []

    new_people, dupes = _filter_new_leads(
        people,
        sheet_emails=sheet_emails,
        seen_ids=seen_ids,
        run_emails=run_emails,
        require_email=require_email,
    )
    if new_people:
        _attach_domains(new_people)
    return new_people, hits, dupes


def find_agencies(page: int = 1, require_email: bool = True) -> list[dict]:
    """Return NEW people from Apollo (broad US multi-search) or CSV.

    - 5 US variants, up to 3 pages × 100 each
    - optional intl variant if US total hits < 20
    - dedupe by email (+ Apollo id / sheet)
    - persist research_state.json run summary
    """
    _ = page  # pagination is internal (pages 1–3 per variant)
    apollo_key = os.environ.get("APOLLO_KEY", "").strip()
    if not apollo_key:
        people = _from_csv()
        if require_email:
            people = [p for p in people if (p.get("email") or "").strip()]
        return people

    state = _load_state()
    sheet_emails = _existing_sheet_emails()
    seen_ids = {str(x) for x in (state.get("last_seen_ids") or []) if x}
    run_emails: set[str] = set()

    variants = get_daily_search_variants()
    print(
        f"Daily broad US search: {len(variants)} variants "
        f"(per_page={PER_PAGE}, max_pages={MAX_PAGES})",
        flush=True,
    )

    all_new: list[dict] = []
    total_hits = 0
    total_dupes = 0
    us_hits = 0

    for i, variant in enumerate(variants, start=1):
        new_people, hits, dupes = _run_one_variant(
            i,
            variant,
            sheet_emails=sheet_emails,
            seen_ids=seen_ids,
            run_emails=run_emails,
            require_email=require_email,
        )
        us_hits += hits
        total_hits += hits
        total_dupes += dupes
        all_new.extend(new_people)
        time.sleep(0.35)

    variants_run = len(variants)
    if us_hits < US_MIN_HITS_BEFORE_INTL:
        intl = get_international_variant()
        variants_run += 1
        print(
            f"US hits={us_hits} < {US_MIN_HITS_BEFORE_INTL} → running international variant",
            flush=True,
        )
        new_people, hits, dupes = _run_one_variant(
            variants_run,
            intl,
            sheet_emails=sheet_emails,
            seen_ids=seen_ids,
            run_emails=run_emails,
            require_email=require_email,
        )
        total_hits += hits
        total_dupes += dupes
        all_new.extend(new_people)

    # Cross-check: final email dedup (safety)
    final: list[dict] = []
    seen_final: set[str] = set()
    for p in all_new:
        email = (p.get("email") or "").strip().lower()
        key = email or str(p.get("id") or "")
        if not key or key in seen_final:
            if key in seen_final:
                total_dupes += 1
            continue
        seen_final.add(key)
        final.append(p)

    # Cap daily additions so the sheet stays manageable; overflow stays
    # eligible for later runs (drop their ids from last_seen_ids).
    if len(final) > NEW_LEADS_CAP:
        overflow = final[NEW_LEADS_CAP:]
        final = final[:NEW_LEADS_CAP]
        for p in overflow:
            pid = str(p.get("id") or "").strip()
            if pid:
                seen_ids.discard(pid)
        print(
            f"Capped NEW leads to {NEW_LEADS_CAP} "
            f"(held back {len(overflow)} for later runs)",
            flush=True,
        )

    _save_run_state(
        variants_run=variants_run,
        total_hits=total_hits,
        new_added=len(final),
        duplicates_skipped=total_dupes,
        last_seen_ids=list(seen_ids),
    )

    print(f"TOTAL NEW AFTER DEDUP: {len(final)}", flush=True)
    return final
