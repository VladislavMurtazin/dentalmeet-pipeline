"""A/B testing for agency email templates — scoring, weighted selection, variants."""

from __future__ import annotations

import json
import random
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PERFORMANCE_PATH = ROOT / "data" / "template_performance.json"
GENERATED_PATH = ROOT / "data" / "generated_templates.json"

MIN_SENDS_PER_TEMPLATE = 100
MIN_TRACKING_DAYS = 30
BASE_TEMPLATE_IDS = [f"A{i}" for i in range(1, 11)]

WEIGHT_TOP = 0.60
WEIGHT_MID = 0.30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _empty_counts() -> dict[str, int]:
    return {"sent": 0, "opened": 0, "replied": 0, "interested": 0}


def _load_json(path: Path, default: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_performance() -> dict:
    raw = _load_json(PERFORMANCE_PATH, {})
    if not isinstance(raw, dict):
        raw = {}
    meta = raw.setdefault("_meta", {})
    meta.setdefault("tracking_started", _utc_now().date().isoformat())
    for tid in BASE_TEMPLATE_IDS:
        raw.setdefault(tid, _empty_counts())
        for k in _empty_counts():
            raw[tid].setdefault(k, 0)
    return raw


def save_performance(data: dict) -> None:
    data["_meta"] = data.get("_meta") or {}
    data["_meta"]["updated_at"] = _utc_now().isoformat(timespec="seconds")
    _save_json(PERFORMANCE_PATH, data)


def load_generated_templates() -> list[dict]:
    raw = _load_json(GENERATED_PATH, {"templates": []})
    if isinstance(raw, dict):
        return list(raw.get("templates") or [])
    return []


def save_generated_templates(templates: list[dict]) -> None:
    _save_json(
        GENERATED_PATH,
        {
            "templates": templates,
            "updated_at": _utc_now().isoformat(timespec="seconds"),
        },
    )


def all_agency_templates(base_templates: list[dict]) -> list[dict]:
    """Base A1–A10 plus generated variants."""
    by_id = {t["id"]: t for t in base_templates}
    for t in load_generated_templates():
        by_id[t["id"]] = t
    return list(by_id.values())


def _ensure_template(data: dict, template_id: str) -> None:
    if template_id not in data or not isinstance(data.get(template_id), dict):
        data[template_id] = _empty_counts()
    for k in _empty_counts():
        data[template_id].setdefault(k, 0)


def record_sent(template_id: str) -> None:
    data = load_performance()
    _ensure_template(data, template_id)
    data[template_id]["sent"] = int(data[template_id].get("sent") or 0) + 1
    save_performance(data)


def record_opened(template_id: str) -> None:
    data = load_performance()
    _ensure_template(data, template_id)
    data[template_id]["opened"] = int(data[template_id].get("opened") or 0) + 1
    save_performance(data)


def record_replied(template_id: str, interested: bool = False) -> None:
    data = load_performance()
    _ensure_template(data, template_id)
    data[template_id]["replied"] = int(data[template_id].get("replied") or 0) + 1
    if interested:
        data[template_id]["interested"] = int(data[template_id].get("interested") or 0) + 1
    save_performance(data)


def _rates(counts: dict[str, int]) -> dict[str, float]:
    sent = int(counts.get("sent") or 0)
    if sent <= 0:
        return {"open_rate": 0.0, "reply_rate": 0.0, "interested_rate": 0.0}
    opened = int(counts.get("opened") or 0)
    replied = int(counts.get("replied") or 0)
    interested = int(counts.get("interested") or 0)
    return {
        "open_rate": round(100.0 * opened / sent, 1),
        "reply_rate": round(100.0 * replied / sent, 1),
        "interested_rate": round(100.0 * interested / sent, 1),
    }


def template_score(counts: dict[str, int]) -> float:
    """score = open_rate*0.3 + reply_rate*0.5 + interested_rate*1.0 (rates in %)."""
    rates = _rates(counts)
    return round(
        rates["open_rate"] * 0.3
        + rates["reply_rate"] * 0.5
        + rates["interested_rate"] * 1.0,
        1,
    )


def _active_template_ids(data: dict | None = None) -> list[str]:
    data = data or load_performance()
    ids = list(BASE_TEMPLATE_IDS)
    for t in load_generated_templates():
        tid = t.get("id")
        if tid and tid not in ids:
            ids.append(tid)
    for key in data:
        if key.startswith("_"):
            continue
        if re.match(r"^A\d+$", key) and key not in ids:
            ids.append(key)
    return ids


def is_ranking_ready(data: dict | None = None) -> bool:
    """Ready after 30 days of tracking or 100+ sends on every base template."""
    data = data or load_performance()
    meta = data.get("_meta") or {}
    started = meta.get("tracking_started")
    if started:
        try:
            start_date = date.fromisoformat(str(started)[:10])
            if (_utc_now().date() - start_date).days >= MIN_TRACKING_DAYS:
                return True
        except ValueError:
            pass

    return all(
        int((data.get(tid) or {}).get("sent") or 0) >= MIN_SENDS_PER_TEMPLATE
        for tid in BASE_TEMPLATE_IDS
    )


def rank_templates(data: dict | None = None) -> list[dict[str, Any]]:
    data = data or load_performance()
    rows: list[dict[str, Any]] = []
    for tid in _active_template_ids(data):
        counts = data.get(tid) or _empty_counts()
        if not isinstance(counts, dict):
            continue
        rates = _rates(counts)
        score = template_score(counts)
        rows.append(
            {
                "template": tid,
                "sent": int(counts.get("sent") or 0),
                "opened": int(counts.get("opened") or 0),
                "replied": int(counts.get("replied") or 0),
                "interested": int(counts.get("interested") or 0),
                **rates,
                "score": score,
            }
        )
    rows.sort(key=lambda r: (r["score"], r["reply_rate"], r["open_rate"]), reverse=True)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def _tier_label(rank: int, total: int) -> str:
    top_n = min(3, total)
    mid_n = min(4, max(0, total - top_n))
    if rank <= top_n:
        return "top performer"
    if rank <= top_n + mid_n:
        return "mid tier"
    return "explore"


def pick_weighted_template(ready_templates: list[dict]) -> tuple[dict, str]:
    """Weighted random: top 3 → 60%, middle 4 → 30%, bottom 3 → 10%."""
    if not ready_templates:
        raise RuntimeError("No templates available for weighted selection")

    rankings = rank_templates()
    rank_by_id = {r["template"]: r for r in rankings}
    ordered = sorted(
        ready_templates,
        key=lambda t: rank_by_id.get(t["id"], {}).get("score", 0),
        reverse=True,
    )
    n = len(ordered)
    if n == 1:
        tpl = ordered[0]
        score = rank_by_id.get(tpl["id"], {}).get("score", 0.0)
        return tpl, f"Selected {tpl['id']} (score: {score}, top performer)"

    top_n = min(3, n)
    mid_n = min(4, max(0, n - top_n))
    top = ordered[:top_n]
    mid = ordered[top_n : top_n + mid_n]
    bottom = ordered[top_n + mid_n :]

    roll = random.random()
    if roll < WEIGHT_TOP and top:
        pool, tier = top, "top performer"
    elif roll < WEIGHT_TOP + WEIGHT_MID and mid:
        pool, tier = mid, "mid tier"
    elif bottom:
        pool, tier = bottom, "explore"
    elif mid:
        pool, tier = mid, "mid tier"
    else:
        pool, tier = top, "top performer"

    tpl = random.choice(pool)
    row = rank_by_id.get(tpl["id"], {})
    score = row.get("score", 0.0)
    rank = row.get("rank", "?")
    log = f"Selected {tpl['id']} (score: {score}, {tier}, rank {rank}/{n})"
    return tpl, log


def sync_from_legacy_stats() -> None:
    """Bootstrap performance from template_stats.json if performance is empty."""
    from .templates import STATS_PATH

    data = load_performance()
    if sum(int((data.get(tid) or {}).get("sent") or 0) for tid in BASE_TEMPLATE_IDS) > 0:
        return
    raw = _load_json(STATS_PATH, {})
    agency = raw.get("agency") if isinstance(raw, dict) else {}
    if not agency:
        return
    for tid, counts in agency.items():
        if not isinstance(counts, dict):
            continue
        _ensure_template(data, tid)
        data[tid]["sent"] = int(counts.get("sent") or 0)
        data[tid]["opened"] = int(counts.get("presentation_clicks") or 0)
    save_performance(data)


def reset_templates() -> None:
    """Clear A/B performance and generated variants."""
    if PERFORMANCE_PATH.exists():
        PERFORMANCE_PATH.unlink()
    if GENERATED_PATH.exists():
        GENERATED_PATH.unlink()
    print("template A/B stats reset (performance + generated variants)")


def print_template_scores() -> None:
    sync_from_legacy_stats()
    data = load_performance()
    ready = is_ranking_ready(data)
    ranked = rank_templates(data)
    print(f"=== Template scores (ranking {'ON' if ready else 'OFF'}) ===")
    for row in ranked:
        tier = _tier_label(row["rank"], len(ranked))
        print(
            f"  #{row['rank']} {row['template']}: score={row['score']} ({tier}) "
            f"sent={row['sent']} open={row['open_rate']}% reply={row['reply_rate']}% "
            f"interested={row['interested_rate']}%"
        )
    print("================================================================")


def _next_variant_ids(count: int = 2) -> list[str]:
    ids = set(_active_template_ids())
    max_n = 10
    for tid in ids:
        match = re.match(r"^A(\d+)$", tid or "")
        if match:
            max_n = max(max_n, int(match.group(1)))
    return [f"A{max_n + i}" for i in range(1, count + 1)]


def _variant_subject(base_subject: str, variant: int) -> str:
    alts = [
        base_subject,
        re.sub(r"^(\w+)", "Quick", base_subject, count=1),
        re.sub(r"\(100% async\)", "(fully async)", base_subject),
        f"{base_subject} — 2-min read",
    ]
    return alts[variant % len(alts)]


def _variant_body(base_body: str, variant: int) -> str:
    cta_pairs = [
        ("Check out this 2-min presentation", "Watch the 2-min presentation"),
        ("Fill out the short form", "Start with the short form"),
        ("See how it works", "See the breakdown"),
        ("Get started", "Start here"),
    ]
    body = base_body
    a, b = cta_pairs[variant % len(cta_pairs)]
    if a in body:
        body = body.replace(a, b)
    elif variant == 1:
        body = body.replace("Fill out the form", "Start here:")
    return body


def maybe_generate_monthly_variants() -> list[str]:
    """Create two variants from top performer once per month (A11, A12, ...)."""
    if not is_ranking_ready():
        return []

    data = load_performance()
    meta = data.get("_meta") or {}
    month_key = _utc_now().strftime("%Y-%m")
    if meta.get("last_variant_month") == month_key:
        return []

    rankings = rank_templates(data)
    if not rankings or int(rankings[0].get("sent") or 0) < 10:
        return []

    from .templates import AGENCY_SIGNATURE, AGENCY_TEMPLATES

    top_id = rankings[0]["template"]
    base = next((t for t in AGENCY_TEMPLATES if t["id"] == top_id), None)
    if base is None:
        for t in load_generated_templates():
            if t.get("id") == top_id:
                base = t
                break
    if base is None:
        return []

    generated = load_generated_templates()
    new_ids = _next_variant_ids(2)
    created: list[str] = []

    for i, new_id in enumerate(new_ids):
        if any(t.get("id") == new_id for t in generated):
            continue
        variant = deepcopy(base)
        variant["id"] = new_id
        variant["subject"] = _variant_subject(base["subject"], i + 1)
        variant["body"] = _variant_body(base["body"], i + 1)
        if AGENCY_SIGNATURE not in variant["body"]:
            variant["body"] = variant["body"].rstrip() + f"\n\n{AGENCY_SIGNATURE}"
        variant["parent"] = top_id
        variant["generated_at"] = _utc_now().isoformat(timespec="seconds")
        generated.append(variant)
        _ensure_template(data, new_id)
        created.append(new_id)

    if created:
        save_generated_templates(generated)
        meta["last_variant_month"] = month_key
        data["_meta"] = meta
        save_performance(data)
        print(f"monthly variants from {top_id}: {', '.join(created)}")

    return created


def template_id_from_notes(notes: str) -> str | None:
    match = re.search(r"tpl=(A\d+)", notes or "", re.I)
    return match.group(1).upper() if match else None
