"""Find agency founders via Apollo API or local CSV fallback.

Two modes:
  DEFAULT / cron  — credit-safe search only (enrichment OFF, 0 credits)
  --enrich N      — batch mode: search all variants, dedup, then enrich ≤N
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

# Cron / default search volume
PER_PAGE = 50
MAX_PAGES = 1
# Batch --enrich volume
BATCH_PER_PAGE = 50
BATCH_MAX_PAGES = 2

US_MIN_HITS_BEFORE_INTL = 20
MAX_SEEN_IDS = 5000
NEW_LEADS_CAP = 30
BULK_BATCH_SIZE = 10

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", re.I)

ENRICHED_SEARCH = "SEARCH"
ENRICHED_TRUE = "TRUE"

DECISION_TITLE_TOKENS = (
    "owner",
    "ceo",
    "founder",
    "president",
    "partner",
    "director",
    "vp",
    "cmo",
    "head of",
    "chief",
)

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
    """Return 3 of 15 variants rotating by day-of-month (cron mode)."""
    day = int(day_of_month if day_of_month is not None else date.today().day)
    base = (max(1, day) - 1) % len(VARIANTS)
    picks = [base, (base + 5) % len(VARIANTS), (base + 10) % len(VARIANTS)]
    return [_variant_from_spec(idx) for idx in picks]


def get_all_search_variants() -> list[dict]:
    """All 15 variants (batch --enrich mode)."""
    return [_variant_from_spec(i) for i in range(len(VARIANTS))]


def _variant_from_spec(idx: int) -> dict:
    spec = VARIANTS[idx]
    return {
        "q_keywords": spec["keyword"],
        "industry": spec["industry"],
        "organization_locations": list(US_LOCATION),
        "person_titles": list(PERSON_TITLES),
        "email_status": ["verified"],
        "region": "US",
        "variant_index": idx,
    }


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


def print_research_plan(
    *, limit: int | None = None, enrich: int | None = None
) -> None:
    today = date.today()
    if enrich:
        variants = get_all_search_variants()
        mode = f"BATCH enrich≤{enrich}"
        pages = BATCH_MAX_PAGES
    else:
        variants = get_daily_search_variants(today.day)
        mode = "credit-safe (enrichment OFF)"
        pages = MAX_PAGES
    cap = NEW_LEADS_CAP if limit is None else min(NEW_LEADS_CAP, max(1, int(limit)))
    print("=== RESEARCH DRY-RUN PLAN (no Apollo API calls) ===", flush=True)
    print(f"Mode: {mode}", flush=True)
    print(f"Date: {today.isoformat()} (day_of_month={today.day})", flush=True)
    print(
        f"per_page={BATCH_PER_PAGE if enrich else PER_PAGE}, "
        f"max_pages={pages}, new_leads_cap={cap}",
        flush=True,
    )
    print(f"Variants ({len(variants)} of {len(VARIANTS)}):", flush=True)
    for i, v in enumerate(variants, start=1):
        print(
            f"  {i}. [{v.get('variant_index')}] keyword={v['q_keywords']!r}",
            flush=True,
        )
    if enrich:
        print(
            "Guards: title filter + sheet dedup (PersonID/LinkedIn/name+org) "
            "BEFORE any enrich call",
            flush=True,
        )
    else:
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
    enrichment: str = "disabled",
    credits_spent: int = 0,
) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date.today().isoformat(),
        "variants_run": variants_run,
        "total_hits": total_hits,
        "new_added": new_added,
        "duplicates_skipped": duplicates_skipped,
        "last_seen_ids": last_seen_ids[-MAX_SEEN_IDS:],
        "enrichment": enrichment,
        "credits_spent": credits_spent,
    }
    STATE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _headers() -> dict:
    return {
        "X-Api-Key": os.environ["APOLLO_KEY"],
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


# ---------------------------------------------------------------------------
# Sheet dedup index (MUST run before any enrichment)
# ---------------------------------------------------------------------------


def _norm_linkedin(url: str) -> str:
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].rstrip("/")
    raw = raw.replace("https://", "").replace("http://", "")
    raw = raw.replace("www.", "")
    return raw


def _identity_key(first: str, last: str, org: str) -> str:
    f = (first or "").strip().lower()
    l = (last or "").strip().lower()
    o = (org or "").strip().lower()
    if not (f or l) or not o:
        return ""
    return f"{f}|{l}|{o}"


def _load_sheet_dedup_index() -> dict[str, set[str]]:
    """Load PersonID / LinkedIn / identity / email sets from Agencies sheet."""
    index = {
        "person_ids": set(),
        "linkedins": set(),
        "identities": set(),
        "emails": set(),
    }
    try:
        from .to_sheets import HEADERS, get_worksheet

        ws = get_worksheet()
        values = ws.get_all_values()
        if not values:
            return index
        header = [(h or "").strip() for h in values[0]]

        def col(name: str) -> int | None:
            try:
                return header.index(name)
            except ValueError:
                # fall back to canonical HEADERS order if header row incomplete
                try:
                    return HEADERS.index(name)
                except ValueError:
                    return None

        i_company = col("Company")
        i_contact = col("Contact")
        i_email = col("Email")
        i_pid = col("PersonID")
        i_li = col("LinkedIn")

        for row in values[1:]:
            def cell(i: int | None) -> str:
                if i is None or i >= len(row):
                    return ""
                return (row[i] or "").strip()

            email = cell(i_email).lower()
            if email:
                index["emails"].add(email)
            pid = cell(i_pid)
            if pid:
                index["person_ids"].add(pid)
            li = _norm_linkedin(cell(i_li))
            if li:
                index["linkedins"].add(li)
            contact = cell(i_contact)
            company = cell(i_company)
            parts = contact.split(None, 1)
            first = parts[0] if parts else ""
            last = parts[1] if len(parts) > 1 else ""
            ident = _identity_key(first, last, company)
            if ident:
                index["identities"].add(ident)
    except Exception as e:
        _log(f"WARN: sheet dedup index unavailable: {e}", level=logging.WARNING)
    return index


def _is_duplicate_before_enrich(p: dict, index: dict[str, set[str]]) -> bool:
    """True if person already in sheet / known — do NOT spend credits."""
    pid = str(p.get("id") or p.get("person_id") or "").strip()
    if pid and pid in index["person_ids"]:
        return True
    li = _norm_linkedin(_person_linkedin(p))
    if li and li in index["linkedins"]:
        return True
    org = p.get("organization") or {}
    org_name = (
        org.get("name") if isinstance(org, dict) else ""
    ) or p.get("organization_name") or ""
    first = (p.get("first_name") or "").strip()
    last = (
        p.get("last_name") or p.get("last_name_obfuscated") or ""
    ).strip()
    ident = _identity_key(first, last, str(org_name))
    if ident and ident in index["identities"]:
        return True
    email = (_person_email(p) or "").lower()
    if email and email in index["emails"]:
        return True
    return False


def _is_decision_maker(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return False
    return any(tok in t for tok in DECISION_TITLE_TOKENS)


# ---------------------------------------------------------------------------
# Apollo search (never spends email credits)
# ---------------------------------------------------------------------------


def _search_apollo(
    *,
    page: int,
    q_keywords: str,
    organization_locations: list[str],
    person_titles: list[str] | None = None,
    industry: str | None = None,
    per_page: int = PER_PAGE,
) -> list[dict]:
    payload: dict = {
        "q_keywords": q_keywords,
        "person_titles": person_titles or list(PERSON_TITLES),
        "organization_locations": organization_locations,
        "organization_num_employees_ranges": list(EMPLOYEE_RANGES),
        "page": page,
        "per_page": per_page,
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


def _search_variant_pages(
    variant: dict, *, per_page: int, max_pages: int
) -> tuple[list[dict], list[tuple[int, int]]]:
    all_people: list[dict] = []
    seen_ids: set[str] = set()
    page_counts: list[tuple[int, int]] = []

    for page in range(1, max_pages + 1):
        try:
            batch = _search_apollo(
                page=page,
                q_keywords=variant["q_keywords"],
                organization_locations=list(variant["organization_locations"]),
                person_titles=list(variant.get("person_titles") or PERSON_TITLES),
                industry=(variant.get("industry") or "").strip() or None,
                per_page=per_page,
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

        if len(batch) < per_page:
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


def _to_person(p: dict, *, enriched: str) -> dict:
    last = (p.get("last_name") or "").strip()
    if not last and p.get("last_name_obfuscated"):
        last = str(p.get("last_name_obfuscated") or "").strip()
    email = _person_email(p)
    status = (p.get("email_status") or "").lower()
    return {
        "id": p.get("id"),
        "person_id": str(p.get("id") or "").strip(),
        "first_name": p.get("first_name") or "",
        "last_name": last,
        "email": email,
        "title": p.get("title") or "",
        "linkedin_url": _person_linkedin(p),
        "city": p.get("city") or "",
        "state": p.get("state") or "",
        "country": p.get("country") or "",
        "verified_email": status == "verified" or bool(email),
        "has_email": p.get("has_email"),
        "enriched": enriched,
        "organization": _org_from_raw(p),
    }


# ---------------------------------------------------------------------------
# Enrichment (ONLY when --enrich N is set)
# ---------------------------------------------------------------------------


def _bulk_match_chunk(details: list[dict]) -> list[dict]:
    """POST bulk_match for one chunk. Raises on hard failure / 422."""
    r = requests.post(
        f"{APOLLO_BASE}/people/bulk_match",
        headers=_headers(),
        params={
            "reveal_personal_emails": "false",
            "reveal_phone_number": "false",
        },
        json={"details": details},
        timeout=60,
    )
    if r.status_code == 429:
        _log("Apollo 429 — sleeping 60s and retrying once", level=logging.WARNING)
        time.sleep(60)
        r = requests.post(
            f"{APOLLO_BASE}/people/bulk_match",
            headers=_headers(),
            params={
                "reveal_personal_emails": "false",
                "reveal_phone_number": "false",
            },
            json={"details": details},
            timeout=60,
        )
    if r.status_code == 422:
        raise requests.exceptions.HTTPError(
            f"422 bulk_match batch of {len(details)}", response=r
        )
    r.raise_for_status()
    matches = r.json().get("matches") or []
    return [m for m in matches if m]


def _enrich_one_by_id(person_id: str) -> dict | None:
    """Fallback single enrich for one Apollo person id."""
    payload = {
        "id": person_id,
        "reveal_personal_emails": False,
        "reveal_phone_number": False,
    }
    for path in ("people/match", "people/enrich"):
        try:
            r = requests.post(
                f"{APOLLO_BASE}/{path}",
                headers=_headers(),
                json=payload,
                timeout=45,
            )
            if r.status_code == 429:
                time.sleep(60)
                r = requests.post(
                    f"{APOLLO_BASE}/{path}",
                    headers=_headers(),
                    json=payload,
                    timeout=45,
                )
            if r.status_code != 200:
                continue
            data = r.json()
            person = data.get("person") or data.get("match") or data
            if isinstance(person, dict) and (
                person.get("id") or _person_email(person)
            ):
                return person
        except Exception:
            continue
    return None


def _enrich_pool(pool: list[dict], *, max_enrich: int) -> tuple[list[dict], int]:
    """Enrich up to max_enrich people. Returns (people_with_email, credits_spent).

    credits_spent ≈ number of people we attempted to enrich.
    """
    to_enrich = pool[: max(0, int(max_enrich))]
    if not to_enrich:
        return [], 0

    credits_spent = 0
    with_email: list[dict] = []
    by_id = {str(p.get("id") or "").strip(): p for p in to_enrich if p.get("id")}

    for i in range(0, len(to_enrich), BULK_BATCH_SIZE):
        chunk = to_enrich[i : i + BULK_BATCH_SIZE]
        details = [{"id": p["id"]} for p in chunk if p.get("id")]
        if not details:
            continue

        credits_spent += len(details)
        matches: list[dict] = []
        try:
            matches = _bulk_match_chunk(details)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            _log(
                f"bulk_match 422/error on batch of {len(details)} "
                f"(status={status}) — one-by-one fallback",
                level=logging.WARNING,
            )
            for d in details:
                pid = str(d.get("id") or "")
                hit = _enrich_one_by_id(pid) if pid else None
                if hit:
                    matches.append(hit)
                time.sleep(0.5)
        except Exception as e:
            _log(f"bulk_match failed: {e} — one-by-one fallback", level=logging.WARNING)
            for d in details:
                pid = str(d.get("id") or "")
                hit = _enrich_one_by_id(pid) if pid else None
                if hit:
                    matches.append(hit)
                time.sleep(0.5)

        for m in matches:
            email = _person_email(m)
            if not email or not _valid_email(email):
                _log("skip after enrich: no email", level=logging.WARNING)
                continue
            pid = str(m.get("id") or "").strip()
            base = by_id.get(pid) or {}
            merged = {**base, **m}
            person = _to_person(merged, enriched=ENRICHED_TRUE)
            person["email"] = email
            person["verified_email"] = True
            person["person_id"] = pid or person.get("person_id") or ""
            with_email.append(person)

        time.sleep(0.25)

    return with_email, credits_spent


# ---------------------------------------------------------------------------
# CSV / filters
# ---------------------------------------------------------------------------


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
        "person_id": "",
        "linkedin_url": "",
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
        pid = str(p.get("id") or p.get("person_id") or "").strip()
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


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _search_collect(
    variants: list[dict], *, per_page: int, max_pages: int
) -> tuple[list[dict], int]:
    """Run search across variants; return (raw people, total_hits)."""
    all_raw: list[dict] = []
    seen: set[str] = set()
    total_hits = 0
    for i, variant in enumerate(variants, start=1):
        found, page_counts = _search_variant_pages(
            variant, per_page=per_page, max_pages=max_pages
        )
        total_hits += len(found)
        _log(
            f"Variant {i} ({_variant_label(variant)}): "
            f"{_format_page_summary(page_counts)}"
        )
        for p in found:
            pid = str(p.get("id") or "").strip()
            if pid and pid in seen:
                continue
            if pid:
                seen.add(pid)
            all_raw.append(p)
        time.sleep(0.35)
    return all_raw, total_hits


def _run_credit_safe(
    *, require_email: bool, limit: int | None
) -> list[dict]:
    """Default / cron: search only, 0 enrichment credits."""
    _log(
        "Credit-safe mode: search with verified filter only, enrichment disabled"
    )
    state = _load_state()
    index = _load_sheet_dedup_index()
    sheet_emails = set(index["emails"])
    seen_ids = {str(x) for x in (state.get("last_seen_ids") or []) if x}
    seen_ids |= set(index["person_ids"])
    run_emails: set[str] = set()

    variants = get_daily_search_variants()
    cap = NEW_LEADS_CAP if limit is None else min(NEW_LEADS_CAP, max(1, int(limit)))
    _log(
        f"Daily rotating US search: {len(variants)} variants "
        f"(per_page={PER_PAGE}, max_pages={MAX_PAGES}, cap={cap})"
    )

    raw, total_hits = _search_collect(
        variants, per_page=PER_PAGE, max_pages=MAX_PAGES
    )
    if total_hits < US_MIN_HITS_BEFORE_INTL:
        _log(
            f"US hits={total_hits} < {US_MIN_HITS_BEFORE_INTL} → international variant"
        )
        intl_raw, intl_hits = _search_collect(
            [get_international_variant()],
            per_page=PER_PAGE,
            max_pages=MAX_PAGES,
        )
        total_hits += intl_hits
        # merge unique
        have = {str(p.get("id") or "") for p in raw}
        for p in intl_raw:
            pid = str(p.get("id") or "")
            if pid and pid not in have:
                raw.append(p)
                have.add(pid)

    people: list[dict] = []
    for p in raw:
        email = _person_email(p)
        if not email or not _valid_email(email):
            _log("skipped: no email in search response", level=logging.WARNING)
            continue
        people.append(_to_person(p, enriched=ENRICHED_SEARCH))

    new_people, dupes = _filter_new_leads(
        people,
        sheet_emails=sheet_emails,
        seen_ids=seen_ids,
        run_emails=run_emails,
        require_email=require_email,
    )
    final = new_people[:cap]

    _save_run_state(
        variants_run=len(variants),
        total_hits=total_hits,
        new_added=len(final),
        duplicates_skipped=dupes,
        last_seen_ids=list(seen_ids),
        enrichment="disabled",
        credits_spent=0,
    )
    _log(f"TOTAL NEW AFTER DEDUP: {len(final)}")
    _log(
        f"All {len(final)} leads have verified email from search "
        f"(0 credits spent)"
    )
    return final


def _run_batch_enrich(*, enrich_limit: int, limit: int | None) -> list[dict]:
    """Manual batch: search all variants → guards → enrich ≤N → save with email."""
    n = max(1, int(enrich_limit))
    save_cap = n if limit is None else min(n, max(1, int(limit)))

    _log(
        f"BATCH MODE: enrich up to {n} NEW decision-makers "
        f"(save_cap={save_cap})"
    )
    _log("Guards: title filter + sheet dedup BEFORE any enrichment call")

    state = _load_state()
    index = _load_sheet_dedup_index()
    seen_ids = {str(x) for x in (state.get("last_seen_ids") or []) if x}
    seen_ids |= set(index["person_ids"])

    variants = get_all_search_variants()
    _log(
        f"Searching all {len(variants)} variants "
        f"(per_page={BATCH_PER_PAGE}, max_pages={BATCH_MAX_PAGES})"
    )
    raw, total_hits = _search_collect(
        variants, per_page=BATCH_PER_PAGE, max_pages=BATCH_MAX_PAGES
    )
    x_raw = len(raw)

    # Title filter
    decision = [p for p in raw if _is_decision_maker(p.get("title") or "")]
    y_decision = len(decision)

    # Dedup BEFORE enrich (critical — no credits on duplicates)
    new_pool: list[dict] = []
    skipped_dup = 0
    for p in decision:
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        if pid in seen_ids or _is_duplicate_before_enrich(p, index):
            skipped_dup += 1
            if pid:
                seen_ids.add(pid)
            continue
        new_pool.append(p)
        seen_ids.add(pid)
        # Reserve identity/linkedin in index so we don't enrich siblings twice
        li = _norm_linkedin(_person_linkedin(p))
        if li:
            index["linkedins"].add(li)
        org = (p.get("organization") or {}).get("name") or ""
        ident = _identity_key(
            p.get("first_name") or "",
            p.get("last_name") or p.get("last_name_obfuscated") or "",
            str(org),
        )
        if ident:
            index["identities"].add(ident)

    z_new = len(new_pool)
    _log(
        f"Pre-enrich pool: raw={x_raw} decision-makers={y_decision} "
        f"new_after_dedup={z_new} (skipped_dup={skipped_dup})"
    )

    if not new_pool:
        _log(
            f"BATCH: found {x_raw} raw → {y_decision} decision-makers → "
            f"{z_new} new after dedup → enriched 0 → 0 with email → saved 0 "
            f"(credits spent: 0)"
        )
        _save_run_state(
            variants_run=len(variants),
            total_hits=total_hits,
            new_added=0,
            duplicates_skipped=skipped_dup,
            last_seen_ids=list(seen_ids),
            enrichment="batch",
            credits_spent=0,
        )
        return []

    enriched_people, credits = _enrich_pool(new_pool, max_enrich=n)
    m_attempted = credits
    w_with_email = len(enriched_people)

    # Final email dedup vs sheet
    sheet_emails = set(index["emails"])
    final: list[dict] = []
    run_emails: set[str] = set()
    for p in enriched_people:
        if len(final) >= save_cap:
            break
        email = (_person_email(p) or "").lower()
        if not email or not _valid_email(email):
            continue
        if email in sheet_emails or email in run_emails:
            continue
        run_emails.add(email)
        sheet_emails.add(email)
        pid = str(p.get("person_id") or p.get("id") or "").strip()
        if pid:
            seen_ids.add(pid)
            index["person_ids"].add(pid)
        final.append(p)

    w_saved = len(final)
    _log(
        f"BATCH: found {x_raw} raw → {y_decision} decision-makers → "
        f"{z_new} new after dedup → enriched {m_attempted} → "
        f"{w_with_email} with email → saved {w_saved} "
        f"(credits spent: {m_attempted})"
    )
    _log(f"TOTAL NEW AFTER DEDUP: {w_saved}")

    _save_run_state(
        variants_run=len(variants),
        total_hits=total_hits,
        new_added=w_saved,
        duplicates_skipped=skipped_dup,
        last_seen_ids=list(seen_ids),
        enrichment="batch",
        credits_spent=m_attempted,
    )
    return final


def find_agencies(
    page: int = 1,
    require_email: bool = True,
    *,
    limit: int | None = None,
    enrich: int | None = None,
) -> list[dict]:
    """Return NEW people.

    - enrich=None → credit-safe search only (cron)
    - enrich=N → batch enrichment of up to N new decision-makers
    """
    _ = page
    apollo_key = os.environ.get("APOLLO_KEY", "").strip()
    if not apollo_key:
        people = _from_csv()
        if require_email:
            people = [p for p in people if (p.get("email") or "").strip()]
        cap = NEW_LEADS_CAP if limit is None else min(NEW_LEADS_CAP, max(1, int(limit)))
        if enrich is not None:
            cap = min(cap, max(1, int(enrich)))
        return people[:cap]

    if enrich is not None and int(enrich) > 0:
        return _run_batch_enrich(enrich_limit=int(enrich), limit=limit)
    return _run_credit_safe(require_email=require_email, limit=limit)
