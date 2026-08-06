"""Find dental practices matching a client's ICP from the Clients sheet."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "targets"
APOLLO_BASE = "https://api.apollo.io/api/v1"

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


def load_client_icp(client_email: str) -> dict:
    """Read ICP filters from Pilot CRM / Clients."""
    from pipeline.to_sheets import connect

    email = client_email.strip().lower()
    gc = connect()
    sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
    ws = sh.worksheet("Clients")
    values = ws.get_all_values()
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
                "practice_types": get(row, "ICP"),
                "geographic_target": get(row, "Geo"),
                "services_needed": get(row, "Services"),
                "calendly": get(row, "Calendly"),
                "pricing": get(row, "Pricing"),
            }
            print(f"ICP loaded for {email}: {icp['practice_types'][:60]!r} / {icp['geographic_target']!r}")
            return icp
    raise RuntimeError(f"Client not found in Clients sheet: {email}")


def find_dental_practices(icp: dict, limit: int = 500) -> list[dict]:
    """Apollo search for dental practice decision-makers matching ICP."""
    practice = (icp.get("practice_types") or "dental").strip()
    geo = (icp.get("geographic_target") or "United States").strip()
    keywords = f"dental {practice}".strip()

    # Normalize geo into list
    locations = [g.strip() for g in geo.replace(";", ",").split(",") if g.strip()]
    if not locations:
        locations = ["United States"]
    if any(x.lower() in ("nationwide", "usa", "us", "united states") for x in locations):
        locations = ["United States"]

    people: list[dict] = []
    page = 1
    per_page = 25

    while len(people) < limit:
        _rate_limit()
        r = requests.post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            headers=_apollo_headers(),
            json={
                "q_keywords": keywords,
                "person_titles": [
                    "Owner",
                    "Founder",
                    "Practice Owner",
                    "Office Manager",
                    "Treatment Coordinator",
                ],
                "organization_locations": locations,
                "organization_num_employees_ranges": ["1-10", "11-50", "51-200"],
                "page": page,
                "per_page": per_page,
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json().get("people") or []
        if not batch:
            break
        people.extend(batch)
        print(f"Apollo search page {page}: +{len(batch)} (total {len(people)})")
        if len(batch) < per_page:
            break
        page += 1

    people = people[:limit]
    # Enrich for emails in chunks of 10
    enriched: list[dict] = []
    for i in range(0, len(people), 10):
        chunk = [p for p in people[i : i + 10] if p.get("id")]
        if not chunk:
            continue
        _rate_limit()
        er = requests.post(
            f"{APOLLO_BASE}/people/bulk_match",
            headers=_apollo_headers(),
            params={"reveal_personal_emails": "false", "reveal_phone_number": "false"},
            json={"details": [{"id": p["id"]} for p in chunk]},
            timeout=60,
        )
        if er.status_code == 403:
            print("WARN: bulk_match forbidden — returning search hits without emails")
            return people[:limit]
        er.raise_for_status()
        matches = [m for m in (er.json().get("matches") or []) if m and m.get("email")]
        enriched.extend(matches)
        print(f"enriched {len(enriched)} with email so far")

    # Prefer verified
    out = []
    for p in enriched:
        status = (p.get("email_status") or "").lower()
        p["verified_email"] = status == "verified"
        if p.get("email"):
            out.append(p)
    return out[:limit]


def enrich_practice(practice: dict) -> dict:
    """Scrape site signal for personalization."""
    from pipeline.personalize import fetch_signal, parse_signal

    org = practice.get("organization") or {}
    domain = (org.get("primary_domain") or "").strip()
    signal = fetch_signal(f"https://{domain}") if domain else ""
    parts = parse_signal(signal)
    practice = dict(practice)
    practice["signal"] = signal
    practice["signal_parts"] = parts
    practice["domain"] = domain
    practice["company"] = org.get("name", "")
    practice["opener"] = ""
    # Lightweight opener from H1/title
    fact = parts.get("H1") or parts.get("Page") or parts.get("Meta") or ""
    fact = " ".join(fact.split())
    if fact:
        if len(fact) > 90:
            fact = fact[:87] + "..."
        practice["opener"] = f"Noticed your practice highlights {fact}."
    return practice


def build_target_list(client_email: str, limit: int = 500) -> list[dict]:
    """ICP → Apollo → enrich → CSV under targets/."""
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    icp = load_client_icp(client_email)
    practices = find_dental_practices(icp, limit=limit)
    rows = []
    for i, p in enumerate(practices, start=1):
        try:
            rows.append(enrich_practice(p))
        except Exception as e:
            print(f"enrich fail {i}: {e}")
            rows.append(p)
        if i % 10 == 0:
            print(f"enriched {i}/{len(practices)}")

    safe = client_email.strip().lower().replace("@", "_at_")
    path = TARGETS_DIR / f"{safe}_targets.csv"
    fieldnames = [
        "email",
        "first_name",
        "last_name",
        "title",
        "company",
        "domain",
        "verified_email",
        "opener",
        "signal",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for p in rows:
            w.writerow(
                {
                    "email": p.get("email", ""),
                    "first_name": p.get("first_name", ""),
                    "last_name": p.get("last_name", ""),
                    "title": p.get("title", ""),
                    "company": p.get("company")
                    or (p.get("organization") or {}).get("name", ""),
                    "domain": p.get("domain")
                    or (p.get("organization") or {}).get("primary_domain", ""),
                    "verified_email": p.get("verified_email", ""),
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
    args = parser.parse_args(argv)
    if args.cmd == "build_list":
        build_target_list(args.client_email, limit=args.limit)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
