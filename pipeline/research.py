"""Find agency founders via Apollo API or local CSV fallback.

CREDIT-SAFE MODE: search with email_status=verified only.
Enrichment (bulk_match / people/enrich) is DISABLED — those spend email credits.
Search hits must already include cleartext verified email.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

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

EMPLOYEE_RANGES = ["1-10", "11-50", "51-200"]
US_LOCATION = ["United States"]
INTL_LOCATIONS = ["United Kingdom", "Canada", "Australia"]

PER_PAGE = 50
MAX_PAGES = 1
US_MIN_HITS_BEFORE_INTL = 20
MAX_SEEN_IDS = 5000
NEW_LEADS_CAP = 30

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", re.I)

# Enriched column value: data came from search, zero email credits spent
ENRICHED_SEARCH = "SEARCH"

VARIANTS = [
    {"keyword": "dental marketing", "industry": "Marketing and Advertising"},
    {"keyword": "dental SEO", "industry": "Marketing and Advertising"},
    {"keyword": "dentist marketing", "industry": "Marketing and Advertising"},
    {"keyword": "dental web design", "industry": "Web Development"},
    {"keyword": "healthcare marketing", "industry": "Marketing and Advertising"},
    {"keyword": "dental advertising", "industry": "Advertising Services"},
    {"keyword": "dental lead generation", "industry": "Marketing and Advertising"},
    {"keyword": "marketing for dentists", "industry": "Marketing and Advertising"},
    {"keyword": "dental patient acquisition", "industry": "Marketing and Advertising"},
    {"keyword": "dental practice growth", "industry": "Marketing and Advertising"},
    {"keyword": "dentist ads", "industry": "Advertising Services"},
    {"keyword": "dental branding", "industry": "Marketing and Advertising"},
    {"keyword": "dental social media", "industry": "Marketing and Advertising"},
    {"keyword": "dental PPC", "industry": "Advertising Services"},
    {"keyword": "smile marketing", "industry": "Marketing and Advertising"},
]


def _log(msg: str, *, level: int = logging.INFO) -> None:
    logger.log(level, msg)
    print(msg, flush=True)


def get_daily_search_variants(day_of_month: int | None = None) -> list[dict]:
    """Return 3 of 15 variants rotating by day-of-month."""
    day = int(day_of_month if day_of_month is not None else date.today().day)
    base = (max(1, day) - 1) % len(VARIANTS)
    picks = [base, (base + 5) % len(VARIANTS), (base + 10) % len(VARIANTS)]
    out: list[dict] = []
    for idx in picks:
        spec = VARIANTS[idx]
        out.append(
            {
                "q_keywords": spec["keyword"],
                "industry": spec["industry"],
                "organization_locations": list(US_LOCATION),
                "person_titles": list(PERSON_TITLES),
                "email_status": ["verified"],
                "region": "US",
                "variant_index": idx,
            }
        )
    return out


def get_international_variant() -> dict:
    return {
        "q_keywords": "dental marketing agency",
        "industry": "Marketing and Advertising",
        "organization_locations": list(INTL_LOCATIONS),
        "person_titles": list(PERSON_TITLES),
        "email_status": ["verified"],
        "region": "UK/CA/AU",
        "variant_index": -1,
    }


def get_daily_search_params(day_of_year: int | None = None) -> dict:
    _ = day_of_year
    v = get_daily_search_variants()[0]
    return {
        "q_keywords": v["q_keywords"],
        "organization_locations": list(v["organization_locations"]),
        "region_code": "US",
        "person_titles": list(v["person_titles"]),
        "email_status": ["verified"],
        "organization_num_employees_ranges": list(EMPLOYEE_RANGES),
    }


def print_research_plan(*, limit: int | None = None) -> None:
    today = date.today()
    variants = get_daily_search_variants(today.day)
    cap = NEW_LEADS_CAP if limit is None else min(NEW_LEADS_CAP, max(1, int(limit)))
    print("=== RESEARCH DRY-RUN PLAN (no Apollo API calls) ===", flush=True)
    print("Credit-safe mode: search with verified filter only, enrichment disabled", flush=True)
    print(f"Date: {today.isoformat()} (day_of_month={today.day})", flush=True)
    print(f"per_page={PER_PAGE}, max_pages={MAX_PAGES}, new_leads_cap={cap}", flush=True)
    print(f"Variants today ({len(variants)} of {len(VARIANTS)}):", flush=True)
    for i, v in enumerate(variants, start=1):
        print(
            f"  {i}. [{v.get('variant_index')}] keyword={v['q_keywords']!r} "
            f"industry={v.get('industry')!r} email_status=['verified']",
            flush=True,
        )
    print("Enrichment: DISABLED (0 email credits)", flush=True)
    print("=== END PLAN ===", flush=True)


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
    payload = {
        "date": date.today().isoformat(),
        "variants_run": variants_run,
        "total_hits": total_hits,
        "new_added": new_added,
        "duplicates_skipped": duplicates_skipped,
        "last_seen_ids": last_seen_ids[-MAX_SEEN_IDS:],
        "credit_safe": True,
        "enrichment": "disabled",
    }
    STATE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _existing_sheet_emails() -> set[str]:
    try:
        from .to_sheets import _existing_emails, get_worksheet

        return _existing_emails(get_worksheet())
    except Exception as e:
        _log(f"WARN: could not load sheet emails for dedup: {e}", level=logging.WARNING)
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
    industry: str | None = None,
    per_page: int = PER_PAGE,
) -> list[dict]:
    """People API search — always requests verified emails (no enrich calls)."""
    status = list(email_status or ["verified"])
    if "verified" not in status:
        status = ["verified"]

    payload: dict = {
        "q_keywords": q_keywords,
        "person_titles": person_titles or list(PERSON_TITLES),
        "organization_locations": organization_locations,
        "organization_num_employees_ranges": organization_num_employees_ranges
        or list(EMPLOYEE_RANGES),
        "page": page,
        "per_page": per_page,
        # Required on every search — cleartext verified emails in response
        "email_status": ["verified"],
        "contact_email_status": ["verified"],
    }
    if industry:
        payload["q_organization_keyword_tags"] = [industry]

    r = requests.post(
        f"{APOLLO_BASE}/mixed_people/api_search",
        headers=_headers(),
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("people", []) or []


def _search_variant_pages(variant: dict) -> tuple[list[dict], list[tuple[int, int]]]:
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
                email_status=["verified"],
                industry=(variant.get("industry") or "").strip() or None,
                per_page=PER_PAGE,
            )
        except Exception as e:
            _log(f"  WARN page {page} failed: {e}", level=logging.WARNING)
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
            break
        time.sleep(0.35)

    return all_people, page_counts


def _format_page_summary(page_counts: list[tuple[int, int]]) -> str:
    if not page_counts:
        return "0 hits"
    counts = list(page_counts)
    if len(counts) > 1 and counts[-1][1] == 0:
        counts = counts[:-1]
    parts = [f"page {p}={n}" for p, n in counts]
    total = sum(n for _, n in counts)
    return f"{', '.join(parts)} → {total} hits"


def _valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def _person_email(p: dict) -> str:
    for key in ("email", "work_email", "primary_email"):
        val = (p.get(key) or "").strip()
        if val and not val.lower().endswith("email_not_unlocked.com"):
            return val
    contact = p.get("contact") if isinstance(p.get("contact"), dict) else {}
    for key in ("email", "work_email"):
        val = (contact.get(key) or "").strip()
        if val and not val.lower().endswith("email_not_unlocked.com"):
            return val
    return ""


def _person_linkedin(p: dict) -> str:
    return str(p.get("linkedin_url") or p.get("linkedin") or "").strip()


def _org_from_raw(p: dict) -> dict:
    org = p.get("organization") or {}
    if not isinstance(org, dict):
        org = {}
    name = (org.get("name") or p.get("organization_name") or "").strip()
    domain = (
        org.get("primary_domain")
        or org.get("website_url")
        or p.get("organization_primary_domain")
        or ""
    )
    domain = str(domain).strip()
    if "://" in domain:
        domain = domain.split("://", 1)[1].split("/", 1)[0]
    return {
        "name": name,
        "primary_domain": domain,
        "id": org.get("id") or p.get("organization_id"),
    }


def _convert_search_hits(found: list[dict]) -> list[dict]:
    """Convert search hits → person dicts. Skip anyone without cleartext email.

    NEVER calls enrichment APIs.
    """
    people: list[dict] = []
    for p in found:
        email = _person_email(p)
        if not email or not _valid_email(email):
            _log("skipped: no email in search response", level=logging.WARNING)
            continue

        org = _org_from_raw(p)
        last = (p.get("last_name") or "").strip()
        if not last and p.get("last_name_obfuscated"):
            last = str(p.get("last_name_obfuscated") or "").strip()

        people.append(
            {
                "id": p.get("id"),
                "first_name": p.get("first_name") or "",
                "last_name": last,
                "email": email,
                "title": p.get("title") or "",
                "linkedin_url": _person_linkedin(p),
                "city": p.get("city") or "",
                "state": p.get("state") or "",
                "country": p.get("country") or "",
                "verified_email": True,
                "has_email": True,
                "enriched": ENRICHED_SEARCH,
                "organization": org,
            }
        )
    return people


def _normalize_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping = {}
    for h in fieldnames or []:
        key = HEADER_MAP.get(h.strip().lower())
        if key:
            mapping[h] = key
    return mapping


def _row_to_person(row: dict, header_map: dict[str, str]) -> dict:
    data = {header_map[k]: (v or "").strip() for k, v in row.items() if k in header_map}
    return {
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "email": data.get("email", ""),
        "title": data.get("title", ""),
        "verified_email": True,
        "enriched": ENRICHED_SEARCH,
        "organization": {
            "name": data.get("organization_name", ""),
            "primary_domain": data.get("organization_primary_domain", ""),
        },
    }


def _from_csv() -> list[dict]:
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
    new_people: list[dict] = []
    dupes = 0
    for p in people:
        pid = str(p.get("id") or "").strip()
        email = (_person_email(p) or "").strip().lower()

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
    """Search only (no enrichment). Returns (new_people, hits, dupes)."""
    label = _variant_label(variant)
    keyword = (variant.get("q_keywords") or label or "?").strip()
    found, page_counts = _search_variant_pages(variant)
    hits = len(found)
    _log(f"Variant {index} ({label}): {_format_page_summary(page_counts)}")
    _log(f"Variant '{keyword}': found {hits} raw people")

    if not found:
        return [], 0, 0

    people = _convert_search_hits(found)
    skipped_no_email = hits - len(people)
    _log(f"  - with verified email in search: {len(people)}")
    _log(f"  - skipped (no email in search response): {skipped_no_email}")
    _log("  - enrichment calls: 0 (disabled)")

    new_people, dupes = _filter_new_leads(
        people,
        sheet_emails=sheet_emails,
        seen_ids=seen_ids,
        run_emails=run_emails,
        require_email=require_email,
    )
    _log(f"  - new after dedup: {len(new_people)}")
    return new_people, hits, dupes


def find_agencies(
    page: int = 1,
    require_email: bool = True,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Return NEW people from Apollo search (credit-safe) or CSV.

    Never calls bulk_match / people/enrich — those spend email credits.
    """
    _ = page
    apollo_key = os.environ.get("APOLLO_KEY", "").strip()
    if not apollo_key:
        people = _from_csv()
        if require_email:
            people = [p for p in people if (p.get("email") or "").strip()]
        cap = NEW_LEADS_CAP if limit is None else min(NEW_LEADS_CAP, max(1, int(limit)))
        return people[:cap]

    _log(
        "Credit-safe mode: search with verified filter only, enrichment disabled"
    )

    state = _load_state()
    sheet_emails = _existing_sheet_emails()
    seen_ids = {str(x) for x in (state.get("last_seen_ids") or []) if x}
    run_emails: set[str] = set()

    variants = get_daily_search_variants()
    cap = NEW_LEADS_CAP if limit is None else min(NEW_LEADS_CAP, max(1, int(limit)))
    _log(
        f"Daily rotating US search: {len(variants)} variants "
        f"(of {len(VARIANTS)}; per_page={PER_PAGE}, max_pages={MAX_PAGES}, "
        f"cap={cap})"
    )

    all_new: list[dict] = []
    total_hits = 0
    total_dupes = 0
    us_hits = 0
    variants_attempted = 0

    for i, variant in enumerate(variants, start=1):
        if len(all_new) >= cap:
            _log(f"Daily cap reached ({cap}), stopping")
            break
        new_people, hits, dupes = _run_one_variant(
            i,
            variant,
            sheet_emails=sheet_emails,
            seen_ids=seen_ids,
            run_emails=run_emails,
            require_email=require_email,
        )
        variants_attempted += 1
        us_hits += hits
        total_hits += hits
        total_dupes += dupes
        room = cap - len(all_new)
        all_new.extend(new_people[:room])
        if len(all_new) >= cap:
            _log(f"Daily cap reached ({cap}), stopping")
            break
        time.sleep(0.35)

    variants_run = variants_attempted
    if len(all_new) < cap and us_hits < US_MIN_HITS_BEFORE_INTL:
        intl = get_international_variant()
        variants_run += 1
        _log(
            f"US hits={us_hits} < {US_MIN_HITS_BEFORE_INTL} → running international variant"
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
        room = cap - len(all_new)
        all_new.extend(new_people[:room])

    final: list[dict] = []
    seen_final: set[str] = set()
    for p in all_new:
        email = (_person_email(p) or "").strip().lower()
        key = email or str(p.get("id") or "")
        if not key or key in seen_final:
            if key in seen_final:
                total_dupes += 1
            continue
        seen_final.add(key)
        # Guarantee Enriched marker
        p["enriched"] = ENRICHED_SEARCH
        final.append(p)

    if len(final) > cap:
        overflow = final[cap:]
        final = final[:cap]
        for p in overflow:
            pid = str(p.get("id") or "").strip()
            if pid:
                seen_ids.discard(pid)
        _log(f"Capped NEW leads to {cap} (held back {len(overflow)} for later runs)")

    _save_run_state(
        variants_run=variants_run,
        total_hits=total_hits,
        new_added=len(final),
        duplicates_skipped=total_dupes,
        last_seen_ids=list(seen_ids),
    )

    _log(f"TOTAL NEW AFTER DEDUP: {len(final)}")
    _log(
        f"All {len(final)} leads have verified email from search "
        f"(0 credits spent)"
    )
    return final
