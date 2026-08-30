"""Multilingual inbound reply classification (keyword scoring)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = ROOT / "data" / "reply_classification_stats.json"

INTERESTED_KEYWORDS: dict[str, list[str]] = {
    "en": [
        "interested",
        "tell me more",
        "yes",
        "sounds good",
        "how much",
        "pricing",
        "book",
        "schedule",
        "let's do it",
        "lets do it",
        "let's work",
        "lets work",
        "great",
        "awesome",
        "love it",
        "sign me up",
        "okay",
        "ok",
        "sure",
        "definitely",
        "absolutely",
        "i'm in",
        "im in",
        "ready to start",
    ],
    "es": [
        "interesado",
        "interesada",
        "dime más",
        "dime mas",
        "sí",
        "si",
        "suena bien",
        "cuánto cuesta",
        "cuanto cuesta",
        "agendar",
        "trabajemos",
        "perfecto",
        "de acuerdo",
        "claro",
        "genial",
    ],
    "fr": [
        "intéressé",
        "interessee",
        "intéressée",
        "dites-moi",
        "oui",
        "ça a l'air bien",
        "ca a l'air bien",
        "combien",
        "réserver",
        "reserver",
        "travaillons",
        "parfait",
        "d'accord",
        "bien sûr",
        "bien sur",
        "génial",
        "genial",
    ],
    "de": [
        "interessiert",
        "erzählen sie mir",
        "erzaehlen sie mir",
        "ja",
        "klingt gut",
        "wie viel",
        "buchen",
        "arbeiten wir",
        "perfekt",
        "einverstanden",
        "klar",
        "super",
    ],
    "ru": [
        "интересно",
        "заинтересован",
        "заинтересована",
        "расскажите",
        "да",
        "звучит хорошо",
        "сколько стоит",
        "давайте работать",
        "отлично",
        "хорошо",
        "конечно",
        "круто",
    ],
}

NOT_INTERESTED_KEYWORDS: dict[str, list[str]] = {
    "en": [
        "not interested",
        "no thanks",
        "no thank you",
        "remove me",
        "unsubscribe",
        "stop",
        "not for me",
        "pass",
        "don't contact",
        "do not contact",
        "opt out",
        "opt-out",
    ],
    "es": [
        "no interesado",
        "no interesada",
        "no gracias",
        "eliminar",
        "baja",
        "no me interesa",
    ],
    "fr": [
        "pas intéressé",
        "pas interesse",
        "pas intéressée",
        "non merci",
        "supprimer",
        "désinscrire",
        "desinscrire",
    ],
    "de": [
        "nicht interessiert",
        "nein danke",
        "abbestellen",
        "entfernen",
    ],
    "ru": [
        "не интересно",
        "нет спасибо",
        "отписаться",
        "не надо",
        "удалите",
    ],
}

OUT_OF_OFFICE_KEYWORDS = [
    "out of office",
    "away message",
    "auto-reply",
    "autoreply",
    "automatic reply",
    "vacation",
    "away until",
    "on leave",
    "ooo:",
    "i am currently out",
    "i'm currently out",
    "currently away",
]

UNSUBSCRIBE_KEYWORDS = [
    "unsubscribe",
    "remove me",
    "opt out",
    "opt-out",
    "stop emailing",
    "отписаться",
    "baja",
    "désinscrire",
    "desinscrire",
    "abbestellen",
]

PAYMENT_KEYWORDS = [
    "payment",
    "pay",
    "invoice",
    "usdt",
    "crypto",
    "wallet",
    "trc20",
    "wire",
    "bank transfer",
    "$700",
    "$150",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _count_hits(text: str, phrases: list[str]) -> int:
    """Count phrase hits; longer phrases preferred (non-overlapping)."""
    remaining = text
    hits = 0
    for p in sorted((x for x in phrases if x), key=len, reverse=True):
        needle = p.lower()
        if needle in remaining:
            hits += remaining.count(needle)
            remaining = remaining.replace(needle, " ")
    return hits


def _scrub_phrases(text: str, phrases: list[str]) -> str:
    out = text
    for p in sorted((x for x in phrases if x), key=len, reverse=True):
        out = out.replace(p.lower(), " ")
    return out


def detect_language(text: str) -> str:
    """Pick language whose keyword set has the most total matches."""
    low = _normalize(text)
    best_lang = "en"
    best_score = -1
    for lang in INTERESTED_KEYWORDS:
        score = _count_hits(low, INTERESTED_KEYWORDS[lang]) + _count_hits(
            low, NOT_INTERESTED_KEYWORDS.get(lang, [])
        )
        # Prefer non-ASCII scripts when present
        if lang == "ru" and re.search(r"[а-яё]", low):
            score += 2
        if score > best_score:
            best_score = score
            best_lang = lang
    if re.search(r"[а-яё]", low):
        return "ru"
    return best_lang


def classify_reply(text: str) -> dict:
    """Classify inbound reply. Returns category, language, and scores.

    Categories: interested | not_interested | question | out_of_office |
                unsubscribe | payment_question | other
    """
    raw = text or ""
    low = _normalize(raw)
    lang = detect_language(raw)

    scores = {
        "interested": 0,
        "not_interested": 0,
        "out_of_office": 0,
        "unsubscribe": 0,
        "payment_question": 0,
        "question": 0,
    }

    # Hard signals first
    ooo_hits = _count_hits(low, OUT_OF_OFFICE_KEYWORDS)
    scores["out_of_office"] = ooo_hits * 3

    unsub_hits = _count_hits(low, UNSUBSCRIBE_KEYWORDS)
    scores["unsubscribe"] = unsub_hits * 4

    # Aggregate multilingual keyword hits (weight matching language higher)
    all_not = []
    for code, phrases in NOT_INTERESTED_KEYWORDS.items():
        w = 2 if code == lang else 1
        scores["not_interested"] += _count_hits(low, phrases) * w
        all_not.extend(phrases)

    # Scrub decline phrases so "interested" inside "not interested" does not fire
    interested_text = _scrub_phrases(low, all_not + UNSUBSCRIBE_KEYWORDS)
    for code, phrases in INTERESTED_KEYWORDS.items():
        w = 2 if code == lang else 1
        scores["interested"] += _count_hits(interested_text, phrases) * w

    pay_hits = _count_hits(low, PAYMENT_KEYWORDS)
    scores["payment_question"] = pay_hits * 2

    if "?" in raw or "¿" in raw or "？" in raw:
        scores["question"] += 2
    # Question words (light signal)
    q_words = (
        "how ",
        "what ",
        "when ",
        "where ",
        "why ",
        "who ",
        "can you",
        "could you",
        "cómo",
        "como ",
        "qué",
        "que ",
        "combien",
        "wie ",
        "как ",
        "сколько",
    )
    if any(q in low for q in q_words):
        scores["question"] += 1

    # Tie-break / priority order for equal scores
    priority = [
        "unsubscribe",
        "out_of_office",
        "not_interested",
        "payment_question",
        "interested",
        "question",
    ]
    best_cat = "other"
    best_score = 0
    for cat in priority:
        sc = scores.get(cat, 0)
        if sc > best_score:
            best_score = sc
            best_cat = cat

    # Ambiguous: interested vs not_interested both scoring → other
    if (
        scores["interested"] > 0
        and scores["not_interested"] > 0
        and abs(scores["interested"] - scores["not_interested"]) <= 1
        and best_cat in ("interested", "not_interested")
    ):
        best_cat = "other"
        best_score = 0

    # Empty / no signal
    if best_score <= 0:
        if low.strip():
            best_cat = "question" if "?" in raw else "other"
        else:
            best_cat = "other"

    result = {
        "category": best_cat,
        "language": lang,
        "scores": scores,
        "confidence": best_score,
    }
    _bump_stats(best_cat, lang)
    return result


def _bump_stats(category: str, language: str) -> None:
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {"by_category": {}, "by_language": {}, "total": 0}
        if STATS_PATH.exists():
            raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        data.setdefault("by_category", {})
        data.setdefault("by_language", {})
        data["by_category"][category] = int(data["by_category"].get(category) or 0) + 1
        data["by_language"][language] = int(data["by_language"].get(language) or 0) + 1
        data["total"] = int(data.get("total") or 0) + 1
        STATS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def load_classification_stats() -> dict:
    if not STATS_PATH.exists():
        return {"by_category": {}, "by_language": {}, "total": 0}
    try:
        raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"by_category": {}, "by_language": {}, "total": 0}
    except (OSError, json.JSONDecodeError):
        return {"by_category": {}, "by_language": {}, "total": 0}


def print_reply_stats() -> None:
    data = load_classification_stats()
    print("=== REPLY CLASSIFICATION STATS ===")
    print(f"Total classified: {data.get('total', 0)}")
    print("By category:")
    for cat, n in sorted(
        (data.get("by_category") or {}).items(), key=lambda x: (-x[1], x[0])
    ):
        print(f"  {cat}: {n}")
    print("By language:")
    for lang, n in sorted(
        (data.get("by_language") or {}).items(), key=lambda x: (-x[1], x[0])
    ):
        print(f"  {lang}: {n}")
    print("=================================")
