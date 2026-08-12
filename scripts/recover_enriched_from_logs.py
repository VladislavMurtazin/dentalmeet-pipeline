"""One-off: recover enriched lead previews from prior terminal logs into jsonl."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.enriched_store import JSONL_PATH, append_enriched_lead, load_all_records

TERMINALS = Path(
    r"C:\Users\79605\.cursor\projects\c-Users-79605-Desktop-Work-lee-leadpipe\terminals"
)
FILES = [TERMINALS / "123706.txt", TERMINALS / "123707.txt"]


def main() -> None:
    seen: set[str] = set()
    n = 0
    for path in FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{") or '"email"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            email = (obj.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            person = {
                "person_id": obj.get("person_id") or "",
                "id": obj.get("person_id") or "",
                "email": obj.get("email") or "",
                "first_name": obj.get("first_name") or "",
                "last_name": obj.get("last_name") or "",
                "title": obj.get("title") or "",
                "linkedin_url": obj.get("linkedin_url") or "",
                "verified_email": bool(obj.get("verified_email", True)),
                "enriched": obj.get("enriched") or "TRUE",
                "organization": {
                    "name": obj.get("company") or "",
                    "primary_domain": obj.get("domain") or "",
                },
            }
            append_enriched_lead(person, synced=False)
            n += 1
    print(f"recovered_unique={n}")
    print(f"jsonl_total={len(load_all_records())}")
    print(f"path={JSONL_PATH}")


if __name__ == "__main__":
    main()
