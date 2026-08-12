"""Local-first enriched leads store + research run lock.

``data/enriched_leads.jsonl`` is the source of truth for enriched people.
Sheets sync is best-effort; failed writes are recovered via ``sync-leads``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
JSONL_PATH = DATA_DIR / "enriched_leads.jsonl"
LOCK_PATH = DATA_DIR / "research.lock"
LOCK_MAX_AGE_SEC = 30 * 60

logger = logging.getLogger(__name__)

_log = print


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm_linkedin(url: str) -> str:
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].rstrip("/")
    raw = raw.replace("https://", "").replace("http://", "").replace("www.", "")
    return raw


def person_to_record(person: dict, *, synced: bool = False) -> dict:
    """Serialize a person dict into a stable jsonl record."""
    org = person.get("organization") or {}
    if not isinstance(org, dict):
        org = {}
    company = (
        org.get("name")
        or person.get("company")
        or person.get("organization_name")
        or ""
    )
    domain = (
        org.get("primary_domain")
        or person.get("primary_domain")
        or person.get("domain")
        or ""
    )
    pid = str(person.get("person_id") or person.get("id") or "").strip()
    return {
        "person_id": pid,
        "email": (person.get("email") or "").strip(),
        "first_name": (person.get("first_name") or "").strip(),
        "last_name": (person.get("last_name") or "").strip(),
        "company": str(company).strip(),
        "title": (person.get("title") or "").strip(),
        "linkedin_url": str(
            person.get("linkedin_url") or person.get("linkedin") or ""
        ).strip(),
        "primary_domain": str(domain).strip(),
        "verified_email": bool(person.get("verified_email", True)),
        "enriched": str(person.get("enriched") or "TRUE"),
        "enriched_at": person.get("enriched_at") or _now_iso(),
        "synced": bool(synced),
    }


def record_to_person(rec: dict) -> dict:
    """Rebuild a person dict suitable for ``add_row``."""
    return {
        "id": rec.get("person_id") or "",
        "person_id": rec.get("person_id") or "",
        "email": rec.get("email") or "",
        "first_name": rec.get("first_name") or "",
        "last_name": rec.get("last_name") or "",
        "title": rec.get("title") or "",
        "linkedin_url": rec.get("linkedin_url") or "",
        "verified_email": bool(rec.get("verified_email", True)),
        "enriched": rec.get("enriched") or "TRUE",
        "organization": {
            "name": rec.get("company") or "",
            "primary_domain": rec.get("primary_domain") or "",
        },
    }


def iter_records() -> Iterator[dict]:
    if not JSONL_PATH.exists():
        return
        yield  # pragma: no cover — makes this a generator
    with JSONL_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def load_all_records() -> list[dict]:
    return list(iter_records())


def load_local_index() -> dict[str, set[str]]:
    """Keys already present locally (for idempotent enrich / dedup)."""
    index = {
        "person_ids": set(),
        "emails": set(),
        "linkedins": set(),
    }
    for rec in iter_records():
        pid = str(rec.get("person_id") or "").strip()
        email = str(rec.get("email") or "").strip().lower()
        li = _norm_linkedin(str(rec.get("linkedin_url") or ""))
        if pid:
            index["person_ids"].add(pid)
        if email:
            index["emails"].add(email)
        if li:
            index["linkedins"].add(li)
    return index


def already_enriched_locally(person: dict, index: dict[str, set[str]] | None = None) -> bool:
    idx = index or load_local_index()
    pid = str(person.get("id") or person.get("person_id") or "").strip()
    if pid and pid in idx["person_ids"]:
        return True
    email = str(person.get("email") or "").strip().lower()
    if email and email in idx["emails"]:
        return True
    li = _norm_linkedin(str(person.get("linkedin_url") or person.get("linkedin") or ""))
    if li and li in idx["linkedins"]:
        return True
    return False


def append_enriched_lead(person: dict, *, synced: bool = False) -> dict:
    """Append one enriched lead to jsonl (source of truth). Returns the record."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = person_to_record(person, synced=synced)
    # Don't re-append exact person_id/email if already present
    existing = load_local_index()
    pid = rec["person_id"]
    email = rec["email"].lower()
    if (pid and pid in existing["person_ids"]) or (
        email and email in existing["emails"]
    ):
        return rec
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _log(
        f"local-save: {rec.get('email') or rec.get('person_id')} "
        f"-> {JSONL_PATH.name}"
    )
    return rec


def _rewrite_records(records: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JSONL_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(JSONL_PATH)


def mark_synced(
    *,
    person_id: str = "",
    email: str = "",
) -> int:
    """Mark matching jsonl rows as synced. Returns count updated."""
    pid = (person_id or "").strip()
    em = (email or "").strip().lower()
    if not pid and not em:
        return 0
    records = load_all_records()
    changed = 0
    for rec in records:
        r_pid = str(rec.get("person_id") or "").strip()
        r_em = str(rec.get("email") or "").strip().lower()
        if (pid and r_pid == pid) or (em and r_em == em):
            if not rec.get("synced"):
                rec["synced"] = True
                changed += 1
    if changed:
        _rewrite_records(records)
    return changed


def unsynced_records() -> list[dict]:
    return [r for r in load_all_records() if not r.get("synced")]


def acquire_research_lock() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < LOCK_MAX_AGE_SEC:
            mins = max(1, int(age // 60))
            raise SystemExit(
                "Another research run is in progress "
                f"(lock age ~{mins}m). Wait or delete {LOCK_PATH} if stale."
            )
        _log(f"Removing stale research.lock (age {int(age)}s)")
        LOCK_PATH.unlink(missing_ok=True)
    LOCK_PATH.write_text(
        json.dumps({"pid": os.getpid(), "started_at": _now_iso()}, indent=2) + "\n",
        encoding="utf-8",
    )


def release_research_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def research_lock(*, enabled: bool = True):
    if not enabled:
        yield
        return
    acquire_research_lock()
    try:
        yield
    finally:
        release_research_lock()


def sync_leads_to_sheet(*, limit: int | None = None) -> dict:
    """Write unsynced local leads to Agencies with retry/backoff."""
    from .to_sheets import add_row_with_retry, load_sheet_identity_index

    pending = unsynced_records()
    if limit is not None:
        pending = pending[: max(0, int(limit))]

    _log(f"sync-leads: {len(pending)} unsynced local lead(s)")
    if not pending:
        return {"pending": 0, "added": 0, "skipped": 0, "failed": 0}

    sheet = load_sheet_identity_index()
    added = skipped = failed = 0

    for i, rec in enumerate(pending, start=1):
        pid = str(rec.get("person_id") or "").strip()
        email = str(rec.get("email") or "").strip().lower()
        li = _norm_linkedin(str(rec.get("linkedin_url") or ""))

        if (
            (pid and pid in sheet["person_ids"])
            or (email and email in sheet["emails"])
            or (li and li in sheet["linkedins"])
        ):
            mark_synced(person_id=pid, email=email)
            skipped += 1
            _log(f"[{i}/{len(pending)}] already in sheet -> marked synced: {email or pid}")
            continue

        person = record_to_person(rec)
        try:
            ok = add_row_with_retry(person, opener="")
        except Exception as e:
            failed += 1
            _log(f"[{i}/{len(pending)}] FAILED {email or pid}: {e}")
            continue

        if ok:
            mark_synced(person_id=pid, email=email)
            if pid:
                sheet["person_ids"].add(pid)
            if email:
                sheet["emails"].add(email)
            if li:
                sheet["linkedins"].add(li)
            added += 1
            _log(f"[{i}/{len(pending)}] added: {email or pid}")
        else:
            # add_row returned False (bad/dup email) — treat as synced to avoid loop
            mark_synced(person_id=pid, email=email)
            skipped += 1
            _log(f"[{i}/{len(pending)}] skipped: {email or pid}")

    summary = {
        "pending": len(pending),
        "added": added,
        "skipped": skipped,
        "failed": failed,
    }
    _log(
        f"sync-leads done: added={added} skipped={skipped} failed={failed}"
    )
    return summary
