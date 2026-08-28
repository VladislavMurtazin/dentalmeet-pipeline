"""Cold email templates (plain text). 100% async — no calls with Vladislav."""

from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_CALENDLY = "https://calendly.com/dentalmeet-agency/new-meeting"
CALENDLY_URL = os.environ.get("CALENDLY_URL", "").strip() or _DEFAULT_CALENDLY

DEFAULT_PRESENTATION = "https://canva.link/n4hed1htcuepmq5"
DEFAULT_BRIEF_FORM = (
    "https://docs.google.com/forms/d/"
    "1-_DIAdCf2sqlhcwnUWsAYYKfAb2ibUddOhKwUCA5W_k/viewform"
)

UNSUBSCRIBE_URL = "https://dentalmeet.agency/unsubscribe"

ROOT = Path(__file__).resolve().parent.parent
ROTATION_PATH = ROOT / "data" / "template_rotation.json"
STATS_PATH = ROOT / "data" / "template_stats.json"

# Warm / onboarding emails — unsubscribe is injected by email_sender
# BEFORE this signature (keeps FSL_BULK_SIG happy + plain text).
SIGNATURE = (
    "— Vladislav\n"
    "Founder, DentalMeet\n"
    "hello@dentalmeet.agency"
)

# Cold agency outreach signature — Founder only, no call CTA.
AGENCY_SIGNATURE = (
    "— Vladislav\n"
    "Founder, DentalMeet\n"
    "dentalmeet.agency"
)

# Unique closing lines (PYZOR): each send picks one at random
CLOSING_LINES = [
    "Hope this finds you well.",
    "Looking forward to hearing from you.",
    "Best regards.",
    "Cheers.",
    "Talk soon.",
    "Thanks for reading.",
    "Appreciate your time.",
    "Wishing you a great week.",
    "Sent from my phone.",
    "Quick note from my side.",
    "Happy to share more if useful.",
    "No rush either way.",
    "Take care.",
    "All the best.",
    "Warm regards.",
    "Thanks again.",
    "Catch you later.",
    "Have a good one.",
    "Rooting for your team.",
    "Glad to connect when it fits.",
    "Just sharing in case it helps.",
    "Keeping this short.",
    "Open to your thoughts.",
    "Feel free to ignore if not relevant.",
    "Here if you need anything.",
    "Appreciate any reply.",
    "Standing by if useful.",
    "Hope the timing is right.",
    "Sharing this quietly.",
    "Simple note — no pitch call.",
    "Thanks for considering.",
    "Would love your take.",
    "Anytime works on our side.",
    "Async on our end.",
    "No meeting needed.",
    "Two-minute read only.",
    "Form takes five minutes.",
    "Happy Monday.",
    "Happy Friday.",
    "Quick bump of value.",
    "Bookmark if useful later.",
    "Saving you a click later.",
    "Sharing the short version.",
    "Plain text on purpose.",
    "Keeping formatting light.",
    "No images, just the offer.",
    "Written between meetings.",
    "Drafted between calls.",
    "One more idea for you.",
    "That's the gist.",
]

SUBJECT_SPICE = [
    "quick",
    "short",
    "honest",
    "simple",
    "open",
    "async",
    "fresh",
    "direct",
    "brief",
    "light",
    "real",
    "clear",
    "timely",
    "handy",
    "useful",
]

AGENCY_DEFAULTS = {
    "first_name": "there",
    "company": "your agency",
    "opener": "",
    "signal": "your recent work",
    "calendly_url": CALENDLY_URL,
    "presentation_url": "",
    "form_url": "",
}

DENTAL_DEFAULTS = {
    "first_name": "there",
    "practice": "your practice",
    "opener": "",
    "service": "new patient cases",
    "city": "your area",
    "competitor": "nearby practices",
    "client_name": "the team",
    "client_company": "our agency",
    "calendly_url": "",
    "client_presentation_url": "",
}

# ---------------------------------------------------------------------------
# Async agency templates A1–A10
# ---------------------------------------------------------------------------

AGENCY_TEMPLATES: list[dict] = [
    {
        "id": "A1",
        "subject": "meetings with practice owners for {company} (100% async)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "We book qualified meetings with dental practice owners for marketing "
            "agencies like {company}. You show up to calls — we handle outreach, "
            "follow-ups, and booking.\n\n"
            "We work 100% async — no meetings with us needed.\n\n"
            "Check out this 2-min presentation to see how it works:\n"
            "{presentation_url}\n\n"
            "Then fill out the short form:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A2",
        "subject": "how {company} can add 15-20 meetings/month (no calls)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Most dental marketing agencies rely on referrals — great when they "
            "come, painful when they don't.\n\n"
            "We add a predictable channel: outbound to practice owners in your "
            "target markets, 15-20 qualified meetings/month.\n\n"
            "No meetings with us — everything is async. Read how it works:\n"
            "{presentation_url}\n\n"
            "Ready to start? Fill out the form:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A3",
        "subject": "cheaper than ads, no retainers",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Paid ads for dental clients cost $3-10k/month with uncertain ROI. "
            "Outbound to practice owners costs a fraction and puts you directly "
            "in front of decision-makers.\n\n"
            "We run it done-for-you for agencies like {company}. No retainers, "
            "no meetings with us — 100% async.\n\n"
            "See how it works:\n"
            "{presentation_url}\n\n"
            "Get started:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A4",
        "subject": "your own outbound team (without hiring)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Hiring SDRs costs $80k/year per seat. We give {company} a white-label "
            "outbound engine: your brand, your Calendly, our execution.\n\n"
            "You sell it to clients as your own service. No meetings with us needed.\n\n"
            "Curious how the economics work?\n"
            "{presentation_url}\n\n"
            "Ready? Fill out the form:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A5",
        "subject": "quick question (no call needed)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "How does {company} currently book discovery calls with new dental "
            "practices?\n\n"
            "We run outbound that delivers 15-20 qualified meetings/month for "
            "dental marketing agencies. Everything is async — no calls with us.\n\n"
            "Happy to share how if relevant:\n"
            "{presentation_url}\n\n"
            "Get started:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A6",
        "subject": "outbound for {company} (2-min read)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Quick one: would 15-20 qualified meetings/month with dental practice "
            "owners move the needle for {company}?\n\n"
            "If yes, check out this 2-min read (no call needed):\n"
            "{presentation_url}\n\n"
            "Then fill out the form:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A7",
        "subject": "how an agency added 12 practice calls last month",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "One agency we work with went from 2 to 12 discovery calls/month with "
            "practice owners — same offer, new channel.\n\n"
            "We'd run the same playbook for {company}. No meetings with us — "
            "everything is async.\n\n"
            "See the details:\n"
            "{presentation_url}\n\n"
            "Ready to start?\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A8",
        "subject": "idea for {company} (no pitch, no call)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "I put together a short presentation on how {company} could add a "
            "steady flow of practice-owner meetings without ads or hiring.\n\n"
            "No pitch, no call — just read this 2-min presentation:\n"
            "{presentation_url}\n\n"
            "If it makes sense, fill out the form:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A9",
        "subject": "{signal} angle for {company}",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Practices investing in {signal} are actively buying marketing help "
            "right now. We can put {company} in front of them before your "
            "competitors do.\n\n"
            "See how it works (no call needed):\n"
            "{presentation_url}\n\n"
            "Get started:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
    {
        "id": "A10",
        "subject": "100% async lead gen for {company}",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "We deliver 15-20 qualified meetings/month with dental practice owners "
            "for agencies like {company}.\n\n"
            "No retainers. No meetings with us. 100% async — you just show up to "
            "the calls we book.\n\n"
            "Read how it works:\n"
            "{presentation_url}\n\n"
            "Fill out the form to start:\n"
            "{form_url}\n\n"
            f"{AGENCY_SIGNATURE}"
        ),
        "requires": ("presentation_url", "form_url"),
    },
]

# ---------------------------------------------------------------------------
# Async dental templates D1–D10 (client sends to practices)
# ---------------------------------------------------------------------------

DENTAL_TEMPLATES: list[dict] = [
    {
        "id": "D1",
        "subject": "new patient consultations for {practice} (100% async)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "We help practices like {practice} add 15-20 new patient consultations "
            "per month with targeted local outreach.\n\n"
            "We work 100% async — no meetings with us needed.\n\n"
            "Check out this 2-min presentation:\n"
            "{client_presentation_url}\n\n"
            "Then book a time:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D2",
        "subject": "how {practice} can add 20 consults/month (no calls)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Referrals dry up without warning. We build a second, predictable "
            "channel: outreach to homeowners in your area who need dental work now.\n\n"
            "No meetings with us — everything is async. Read how it works:\n"
            "{client_presentation_url}\n\n"
            "Ready? Book a time:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D3",
        "subject": "cheaper than ads, no retainers",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Paid ads cost $3-10k/month with uncertain ROI. Our outreach delivers "
            "15-20 consultations/month for a fraction of ad spend.\n\n"
            "No retainers, no meetings with us — 100% async.\n\n"
            "See how it works:\n"
            "{client_presentation_url}\n\n"
            "Get started:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D4",
        "subject": "quick question (no call needed)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "How is {practice} currently filling the calendar — referrals, ads, or "
            "something else?\n\n"
            "We specialize in predictable patient flow. Everything is async — "
            "no calls with us.\n\n"
            "Happy to share how:\n"
            "{client_presentation_url}\n\n"
            "Book a time:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D5",
        "subject": "{service} cases for {practice}",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "High-value cases like {service} are exactly what our outreach targets. "
            "We put {practice} in front of patients actively researching them.\n\n"
            "No meetings with us needed. See how:\n"
            "{client_presentation_url}\n\n"
            "Ready?\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D6",
        "subject": "patient flow for {practice} (2-min read)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Quick one: would 15-20 extra consultations/month change anything for "
            "{practice} this quarter?\n\n"
            "If yes, check out this 2-min read (no call needed):\n"
            "{client_presentation_url}\n\n"
            "Then book a time:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D7",
        "subject": "how a practice like {practice} added 20 consults/month",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "A practice similar to {practice} went from 8 to 28 consultations/month "
            "in 90 days with our outreach system.\n\n"
            "We'd run the same playbook for {practice}. No meetings with us — "
            "everything is async.\n\n"
            "See the details:\n"
            "{client_presentation_url}\n\n"
            "Ready to start?\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D8",
        "subject": "idea for {practice} (no pitch, no call)",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "I sketched a short plan for how {practice} could add a steady stream "
            "of new patient consultations without ads.\n\n"
            "No pitch, no call — just read this 2-min presentation:\n"
            "{client_presentation_url}\n\n"
            "If it makes sense, book a time:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D9",
        "subject": "what {competitor} is doing in {city}",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "Practices in {city} are investing heavily in patient acquisition right "
            "now. We help {practice} win that race with targeted outreach.\n\n"
            "See how it works (no call needed):\n"
            "{client_presentation_url}\n\n"
            "Get started:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
    {
        "id": "D10",
        "subject": "100% async patient flow for {practice}",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "We deliver 15-20 new patient consultations/month for practices like "
            "{practice}.\n\n"
            "No retainers. No meetings with us. 100% async — you just show up to "
            "the calls we book.\n\n"
            "Read how it works:\n"
            "{client_presentation_url}\n\n"
            "Book a time to start:\n"
            "{calendly_url}\n\n"
            "— {client_name}\n"
            "{client_company}"
        ),
        "requires": ("client_presentation_url", "calendly_url"),
    },
]


def presentation_url() -> str:
    return os.environ.get("PRESENTATION_URL", "").strip() or DEFAULT_PRESENTATION


def form_url() -> str:
    return os.environ.get("BRIEF_FORM_URL", "").strip() or DEFAULT_BRIEF_FORM


def client_presentation_url(client_info: dict | None = None) -> str:
    """Client-specific presentation URL (falls back to agency default)."""
    if client_info:
        custom = (client_info.get("presentation_url") or "").strip()
        if custom:
            return custom
    return presentation_url()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def _with_template_utm(url: str, template_id: str) -> str:
    url = (url or "").strip()
    if not url or not template_id:
        return url
    if "utm_source=" in url.lower():
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source={template_id}&utm_medium=email"


def _safe_format(template: str, values: dict) -> str:
    """Format template; missing keys become empty strings."""
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val or ""))
    # Strip unfilled placeholders
    out = re.sub(r"\{[a-z_]+\}", "", out)
    return out


def _normalize_opener(opener: str) -> str:
    return (opener or "").strip()


def _random_closing() -> str:
    return random.choice(CLOSING_LINES)


def _spice_subject(subject: str) -> str:
    """Make subject unique per send without changing the core meaning."""
    subj = (subject or "").strip()
    if not subj:
        return subj
    word = random.choice(SUBJECT_SPICE)
    # ~50/50 prefix vs light suffix so fingerprints diverge
    if random.random() < 0.5:
        return f"{word}: {subj}"
    if "(" in subj:
        return subj
    return f"{subj} — {word}"


def _with_unique_closing(body: str) -> str:
    """Append a random human closing line above the signature (PYZOR)."""
    text = (body or "").rstrip()
    closing = _random_closing()
    # Insert above last em-dash signature if present
    m = list(re.finditer(r"(?m)^[—\-]\s+\S", text))
    if m:
        idx = m[-1].start()
        before = text[:idx].rstrip()
        after = text[idx:]
        return f"{before}\n\n{closing}\n\n{after}"
    return f"{text}\n\n{closing}\n"


def render_agency_template(template: dict, **kwargs) -> tuple[str, str]:
    """Render one agency template. Returns (subject, body)."""
    tpl_id = template.get("id", "A?")
    values = dict(AGENCY_DEFAULTS)
    values.update(kwargs)
    values["first_name"] = (values.get("first_name") or "").strip() or "there"
    values["company"] = (values.get("company") or "").strip() or "your agency"
    values["opener"] = _normalize_opener(values.get("opener", ""))
    values["signal"] = (values.get("signal") or "").strip() or "your recent work"
    values["presentation_url"] = _with_template_utm(
        values.get("presentation_url") or presentation_url(), tpl_id
    )
    values["form_url"] = _with_template_utm(
        values.get("form_url") or form_url(), tpl_id
    )
    values["calendly_url"] = values.get("calendly_url") or CALENDLY_URL

    subject = _spice_subject(_safe_format(template["subject"], values))
    body = _safe_format(template["body"], values)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = _with_unique_closing(body)
    return subject, body


def render_dental_template(template: dict, **kwargs) -> tuple[str, str]:
    """Render one dental template. Returns (subject, body)."""
    tpl_id = template.get("id", "D?")
    values = dict(DENTAL_DEFAULTS)
    values.update(kwargs)
    values["first_name"] = (values.get("first_name") or "").strip() or "there"
    values["practice"] = (values.get("practice") or "").strip() or "your practice"
    values["opener"] = _normalize_opener(values.get("opener", ""))
    values["service"] = (values.get("service") or "").strip() or "new patient cases"
    values["city"] = (values.get("city") or "").strip() or "your area"
    values["competitor"] = (values.get("competitor") or "").strip() or "nearby practices"
    values["client_name"] = (values.get("client_name") or "").strip() or "the team"
    values["client_company"] = (values.get("client_company") or "").strip() or "our agency"
    values["client_presentation_url"] = _with_template_utm(
        values.get("client_presentation_url") or presentation_url(), tpl_id
    )
    values["calendly_url"] = values.get("calendly_url") or ""

    subject = _spice_subject(_safe_format(template["subject"], values))
    body = _safe_format(template["body"], values)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = _with_unique_closing(body)
    return subject, body


def _template_ready(template: dict, values: dict) -> bool:
    for key in template.get("requires") or ():
        if not (values.get(key) or "").strip():
            return False
    return True


def _load_rotation() -> dict:
    if not ROTATION_PATH.exists():
        return {"agency_index": 0, "dental_index": 0}
    try:
        data = json.loads(ROTATION_PATH.read_text(encoding="utf-8"))
        return {
            "agency_index": int(data.get("agency_index") or 0) % 10,
            "dental_index": int(data.get("dental_index") or 0) % 10,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"agency_index": 0, "dental_index": 0}


def _save_rotation(state: dict) -> None:
    ROTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_PATH.write_text(
        json.dumps(
            {
                "agency_index": int(state.get("agency_index") or 0) % 10,
                "dental_index": int(state.get("dental_index") or 0) % 10,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _agency_values(**kwargs) -> dict:
    return {
        "presentation_url": presentation_url(),
        "form_url": form_url(),
        "calendly_url": CALENDLY_URL,
        **kwargs,
    }


def _dental_values(client_info: dict, **kwargs) -> dict:
    cal = (client_info.get("calendly") or "").strip()
    return {
        "client_name": (client_info.get("name") or "").strip() or "the team",
        "client_company": (client_info.get("company") or "").strip() or "our agency",
        "client_presentation_url": client_presentation_url(client_info),
        "calendly_url": cal,
        **kwargs,
    }


def pick_agency_template(start_index: int | None = None) -> tuple[dict, int, str]:
    """Pick agency template — weighted A/B when ranking ready, else round-robin."""
    from .template_ab import all_agency_templates, is_ranking_ready, pick_weighted_template

    templates = all_agency_templates(AGENCY_TEMPLATES)
    values = _agency_values()
    ready = [t for t in templates if _template_ready(t, values)]
    if not ready:
        raise RuntimeError(
            "No agency template available — set PRESENTATION_URL and BRIEF_FORM_URL in .env"
        )

    if is_ranking_ready():
        tpl, log = pick_weighted_template(ready)
        idx = next(i for i, t in enumerate(templates) if t["id"] == tpl["id"])
        return tpl, idx, log

    state = _load_rotation()
    base = int(start_index if start_index is not None else state["agency_index"]) % max(
        len(templates), 1
    )
    for offset in range(len(templates)):
        idx = (base + offset) % len(templates)
        tpl = templates[idx]
        if _template_ready(tpl, values):
            log = f"Selected {tpl['id']} (round-robin, A/B warming up)"
            return tpl, idx, log
    tpl = ready[0]
    idx = next(i for i, t in enumerate(templates) if t["id"] == tpl["id"])
    return tpl, idx, f"Selected {tpl['id']} (fallback)"


def pick_dental_template(
    client_info: dict,
    start_index: int | None = None,
) -> tuple[dict, int]:
    """Pick next usable dental template for a client campaign."""
    state = _load_rotation()
    base = int(start_index if start_index is not None else state["dental_index"]) % 10
    values = _dental_values(client_info)
    for offset in range(len(DENTAL_TEMPLATES)):
        idx = (base + offset) % len(DENTAL_TEMPLATES)
        tpl = DENTAL_TEMPLATES[idx]
        if _template_ready(tpl, values):
            return tpl, idx
    raise RuntimeError(
        f"No dental template available for {client_info.get('email')} — "
        "need Calendly + presentation URL"
    )


def advance_agency_rotation(used_index: int) -> None:
    state = _load_rotation()
    from .template_ab import all_agency_templates

    n = len(all_agency_templates(AGENCY_TEMPLATES))
    state["agency_index"] = (int(used_index) + 1) % max(n, 1)
    _save_rotation(state)


def advance_dental_rotation(used_index: int) -> None:
    state = _load_rotation()
    state["dental_index"] = (int(used_index) + 1) % len(DENTAL_TEMPLATES)
    _save_rotation(state)


def build_agency_email(**kwargs) -> tuple[str, str, str, str]:
    """Build agency first-touch email. Returns (subject, body, template_id, selection_log)."""
    tpl, idx, log = pick_agency_template()
    subject, body = render_agency_template(tpl, **_agency_values(**kwargs))
    advance_agency_rotation(idx)
    record_template_sent("agency", tpl["id"])
    return subject, body, tpl["id"], log


def build_dental_email(client_info: dict, **kwargs) -> tuple[str, str, str]:
    """Build dental first-touch email. Returns (subject, body, template_id)."""
    tpl, idx = pick_dental_template(client_info)
    subject, body = render_dental_template(tpl, **_dental_values(client_info, **kwargs))
    advance_dental_rotation(idx)
    record_template_sent("dental", tpl["id"])
    return subject, body, tpl["id"]


# ---------------------------------------------------------------------------
# Template stats (async conversion metrics)
# ---------------------------------------------------------------------------

def _empty_stats() -> dict:
    agency = {t["id"]: {"sent": 0, "presentation_clicks": 0, "form_submissions": 0} for t in AGENCY_TEMPLATES}
    dental = {t["id"]: {"sent": 0, "presentation_clicks": 0, "form_submissions": 0} for t in DENTAL_TEMPLATES}
    return {"agency": agency, "dental": dental, "updated_at": ""}


def _load_stats() -> dict:
    if not STATS_PATH.exists():
        return _empty_stats()
    try:
        raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        base = _empty_stats()
        for kind in ("agency", "dental"):
            for tid, counts in (raw.get(kind) or {}).items():
                if tid in base[kind]:
                    base[kind][tid].update(
                        {
                            "sent": int(counts.get("sent") or 0),
                            "presentation_clicks": int(counts.get("presentation_clicks") or 0),
                            "form_submissions": int(counts.get("form_submissions") or 0),
                        }
                    )
        return base
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_stats()


def _save_stats(stats: dict) -> None:
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    stats["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATS_PATH.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")


def record_template_sent(kind: str, template_id: str) -> None:
    stats = _load_stats()
    bucket = stats.get(kind, {})
    if template_id in bucket:
        bucket[template_id]["sent"] = int(bucket[template_id].get("sent") or 0) + 1
    else:
        bucket.setdefault(template_id, {"sent": 0, "presentation_clicks": 0, "form_submissions": 0})
        bucket[template_id]["sent"] = int(bucket[template_id].get("sent") or 0) + 1
    _save_stats(stats)
    if kind == "agency":
        from .template_ab import record_sent

        record_sent(template_id)


def record_presentation_click(kind: str, template_id: str) -> None:
    stats = _load_stats()
    bucket = stats.get(kind, {})
    if template_id in bucket:
        bucket[template_id]["presentation_clicks"] = (
            int(bucket[template_id].get("presentation_clicks") or 0) + 1
        )
    _save_stats(stats)
    from .alerts import bump_daily_metric

    bump_daily_metric("emails_opened")
    if kind == "agency":
        from .template_ab import record_opened

        record_opened(template_id)


def record_form_submission(kind: str, template_id: str) -> None:
    stats = _load_stats()
    bucket = stats.get(kind, {})
    if template_id in bucket:
        bucket[template_id]["form_submissions"] = (
            int(bucket[template_id].get("form_submissions") or 0) + 1
        )
    _save_stats(stats)


def print_template_stats() -> None:
    """Print async conversion metrics per template."""
    stats = _load_stats()
    print("=== Template stats (async) ===")
    for kind, label in (("agency", "Agency"), ("dental", "Dental")):
        print(f"\n{label}:")
        for tid, c in sorted(stats.get(kind, {}).items()):
            print(
                f"  {tid}: sent={c.get('sent', 0)} "
                f"clicks={c.get('presentation_clicks', 0)} "
                f"forms={c.get('form_submissions', 0)}"
            )
    updated = stats.get("updated_at") or "never"
    print(f"\n(updated {updated})")


# ---------------------------------------------------------------------------
# Back-compat wrappers + async follow-ups
# ---------------------------------------------------------------------------

def FIRST_EMAIL_TEMPLATE(
    first_name: str,
    company: str,
    opener: str,
    calendly_url: str = CALENDLY_URL,
    signal: str = "",
) -> tuple[str, str]:
    """Legacy wrapper — uses rotating async agency templates."""
    _ = calendly_url
    subject, body, _tid, _log = build_agency_email(
        first_name=first_name,
        company=company,
        opener=opener,
        signal=signal or "your recent work",
    )
    return subject, body


def FOLLOWUP_1_TEMPLATE(
    first_name: str,
    company: str = "",
    calendly_url: str = CALENDLY_URL,
) -> tuple[str, str]:
    """Async bump ~3 days later."""
    _ = calendly_url
    name = (first_name or "").strip() or "there"
    company_name = (company or "").strip()
    about = f" for {company_name}" if company_name else ""
    preso = presentation_url()
    form = form_url()
    subject = _spice_subject(f"quick bump{about} (no call needed)")
    body = (
        f"Hi {name},\n\n"
        f"Just bumping this — still curious about qualified meetings with "
        f"dental practice owners?\n\n"
        f"We work 100% async. Read how it works:\n"
        f"{preso}\n\n"
        f"Fill out the short form:\n"
        f"{form}\n\n"
        f"{SIGNATURE}"
    )
    return subject, _with_unique_closing(body)


def FOLLOWUP_2_TEMPLATE(
    first_name: str,
    company: str = "",
    calendly_url: str = CALENDLY_URL,
) -> tuple[str, str]:
    """Final async note ~7 days later."""
    _ = calendly_url
    name = (first_name or "").strip() or "there"
    company_name = (company or "").strip()
    about = f" / {company_name}" if company_name else ""
    preso = presentation_url()
    form = form_url()
    subject = _spice_subject(f"closing the loop{about}")
    body = (
        f"Hi {name},\n\n"
        f"Last note from me — no pressure either way.\n\n"
        f"If booking qualified meetings with dental practices is still on "
        f"your radar, read the 2-min presentation (no call needed):\n"
        f"{preso}\n\n"
        f"Or fill out the form to start async:\n"
        f"{form}\n\n"
        f"Otherwise I'll assume the timing isn't right and won't follow up again.\n\n"
        f"{SIGNATURE}"
    )
    return subject, _with_unique_closing(body)


def validate_template_word_counts(max_words: int = 130) -> list[str]:
    """Dev helper: return template ids over max_words (sample render)."""
    over: list[str] = []
    sample_agency = _agency_values(
        first_name="Alex",
        company="Acme Dental Marketing",
        opener="Noticed your recent work with implant clients.",
        signal="implant marketing",
    )
    sample_dental = _dental_values(
        {"name": "Jane", "company": "Bright Smiles Agency", "calendly": CALENDLY_URL},
        first_name="Sam",
        practice="Smile Studio",
        opener="Noticed your Invisalign page.",
    )
    for tpl in AGENCY_TEMPLATES:
        _, body = render_agency_template(tpl, **sample_agency)
        if _word_count(body) > max_words:
            over.append(f"{tpl['id']} ({_word_count(body)} words)")
    for tpl in DENTAL_TEMPLATES:
        _, body = render_dental_template(tpl, **sample_dental)
        if _word_count(body) > max_words:
            over.append(f"{tpl['id']} ({_word_count(body)} words)")
    return over
