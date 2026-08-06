"""Classify inbound replies, draft AI answers, send payment instructions."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from .email_sender import send_email
from .to_sheets import is_email

ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = ROOT / "data" / "drafts"
SUPPRESSION_FILE = ROOT / "data" / "suppression.txt"

DEFAULT_CALENDLY = "https://calendly.com/dentalmeet-agency/new-meeting/"

SYSTEM_PROMPT = """
You are a helpful assistant for DentalMeet, a B2B lead generation platform
for dental marketing agencies.

Reply in English, professional but friendly tone.
Keep replies SHORT (max 3-4 sentences).
Use simple language.

Common questions and answers:
- "How do you find contacts?" -> "We use Apollo.io + custom enrichment,
  targeting practice owners and decision-makers only."
- "What if I don't get meetings?" -> "You only pay for booked meetings.
  No results = no payment."
- "Can I see email examples?" -> "Sure! Here are 3 examples from recent campaigns."
- "How long until results?" -> "First meetings typically book in 7-10 days."
- "Do you offer payment methods besides crypto?" ->
  "For the pilot, we accept USDT (TRC20) for instant settlement.
  Wire transfer is possible but adds $50-100 in fees."
- "Can we do a call?" -> "We work 100% async. But if you prefer,
  book here: {calendly_url}"

For questions not in the list, say:
"Great question! Let me check and get back to you within a few hours."
""".strip()

UNSUB_KW = ("unsubscribe", "remove me", "opt out", "opt-out", "stop emailing")
NOT_INTERESTED_KW = ("not interested", "no thanks", "no thank you", "pass", "don't contact")
PAYMENT_KW = (
    "payment",
    "pay",
    "invoice",
    "usdt",
    "crypto",
    "wallet",
    "trc20",
    "wire",
    "bank",
    "how much",
    "pricing",
    "$700",
    "$150",
)
INTERESTED_KW = (
    "interested",
    "tell me more",
    "let's do it",
    "lets do it",
    "sign me up",
    "ready to start",
    "sounds good",
    "i'm in",
    "im in",
    "sure",
    "yes",
)


def _calendly() -> str:
    return os.environ.get("CALENDLY_URL", "").strip() or DEFAULT_CALENDLY


def _wallet() -> str:
    return os.environ.get("TRC20_WALLET_ADDRESS", "").strip()


def classify_incoming_email(email_body: str) -> str:
    """Categorize inbound message for routing."""
    low = (email_body or "").lower()

    if any(k in low for k in UNSUB_KW):
        return "unsubscribe"
    if any(k in low for k in NOT_INTERESTED_KW):
        return "not_interested"
    if any(k in low for k in PAYMENT_KW):
        return "payment_question"
    if any(k in low for k in INTERESTED_KW):
        return "interested"
    if "?" in (email_body or ""):
        return "question"
    return "question" if low.strip() else "other"


def add_to_suppression(email_addr: str) -> None:
    """Append email to local suppression list (do not re-mail)."""
    email_addr = (email_addr or "").strip().lower()
    if not is_email(email_addr):
        return
    SUPPRESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if SUPPRESSION_FILE.exists():
        existing = {
            line.strip().lower()
            for line in SUPPRESSION_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    if email_addr in existing:
        return
    with SUPPRESSION_FILE.open("a", encoding="utf-8") as f:
        f.write(email_addr + "\n")
    print(f"suppression: added {email_addr}")


def is_suppressed(email_addr: str) -> bool:
    email_addr = (email_addr or "").strip().lower()
    if not SUPPRESSION_FILE.exists():
        return False
    return email_addr in {
        line.strip().lower()
        for line in SUPPRESSION_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _call_gemini(prompt: str) -> str:
    key = os.environ.get("GEMINI_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_KEY not set")
    models = ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-latest")
    last_err = None
    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}"
        )
        try:
            r = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
                },
                timeout=45,
            )
            if r.status_code >= 400:
                last_err = f"{model} HTTP {r.status_code}"
                continue
            data = r.json()
            parts = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            last_err = f"{model} empty response"
        except Exception as e:
            last_err = f"{model}: {type(e).__name__}"
    raise RuntimeError(last_err or "Gemini failed")


def _call_openrouter_qwen(prompt: str) -> str:
    """Optional fallback if QWEN_KEY / OPENROUTER key is set."""
    key = (
        os.environ.get("QWEN_KEY", "").strip()
        or os.environ.get("OPENROUTER_KEY", "").strip()
    )
    if not key:
        raise RuntimeError("No QWEN_KEY/OPENROUTER_KEY")
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
    resp = client.chat.completions.create(
        model="qwen/qwen-max",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(calendly_url=_calendly())},
            {"role": "user", "content": prompt},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _template_fallback(incoming_email: str) -> str:
    """Deterministic short reply when no AI key is available."""
    low = (incoming_email or "").lower()
    cal = _calendly()
    if "contact" in low or "apollo" in low or "find" in low:
        return (
            "We use Apollo.io + custom enrichment, targeting practice owners "
            "and decision-makers only. Happy to share more detail if useful."
        )
    if "pay" in low or "meeting" in low and "don't" in low:
        return (
            "You only pay for booked meetings. No results = no payment. "
            "Happy to walk through the pilot pricing if you want."
        )
    if "call" in low or "zoom" in low or "meet" in low:
        return (
            f"We work 100% async. If you prefer a quick call, book here: {cal}"
        )
    if "how long" in low or "timeline" in low or "when" in low:
        return "First meetings typically book in 7-10 days after launch."
    return (
        "Great question! Let me check and get back to you within a few hours."
    )


def draft_reply(incoming_email: str, lead_context: dict | None = None) -> str:
    """Draft a short reply via Gemini (preferred) or Qwen/OpenRouter; else template."""
    lead_context = lead_context or {}
    ctx = (
        f"Lead: {lead_context.get('contact', '')} at {lead_context.get('company', '')}\n"
        f"Email: {lead_context.get('email', '')}\n"
        f"Title: {lead_context.get('title', '')}\n"
    )
    user_prompt = (
        f"{ctx}\n"
        f"Their message:\n{incoming_email}\n\n"
        f"Write the reply body only (no subject line)."
    )
    system = SYSTEM_PROMPT.format(calendly_url=_calendly())
    full_prompt = f"{system}\n\n{user_prompt}"

    try:
        if os.environ.get("GEMINI_KEY", "").strip():
            return _call_gemini(full_prompt)
        return _call_openrouter_qwen(user_prompt)
    except Exception as e:
        print(f"draft_reply AI fallback ({e})")
        return _template_fallback(incoming_email)


def save_draft(
    lead_email: str,
    incoming: str,
    draft: str,
    lead_context: dict | None = None,
) -> Path:
    """Save draft to data/drafts for manual review (not auto-sent)."""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9_.+-]", "_", (lead_email or "unknown").lower())
    path = DRAFTS_DIR / f"{ts}_{safe}.txt"
    ctx = lead_context or {}
    content = (
        f"TO: {lead_email}\n"
        f"COMPANY: {ctx.get('company', '')}\n"
        f"CONTACT: {ctx.get('contact', '')}\n"
        f"CREATED: {ts}\n"
        f"{'=' * 40}\n"
        f"INCOMING:\n{incoming}\n"
        f"{'=' * 40}\n"
        f"DRAFT REPLY:\n{draft}\n"
    )
    path.write_text(content, encoding="utf-8")
    print(f"draft saved: {path}")
    return path


def send_payment_instructions(lead_email: str, first_name: str) -> bool:
    """Send USDT TRC20 payment details for the $700 pilot."""
    email = (lead_email or "").strip()
    if not is_email(email):
        print(f"send_payment_instructions: bad email {email!r}")
        return False
    wallet = _wallet()
    if not wallet:
        print("send_payment_instructions: TRC20_WALLET_ADDRESS missing in .env")
        return False

    name = (first_name or "").strip() or "there"
    subject = "payment details"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Thanks for moving forward with DentalMeet!\n"
        f"\n"
        f"Here are the payment details:\n"
        f"\n"
        f"AMOUNT: $700 (flat pilot pricing)\n"
        f"\n"
        f"PAYMENT METHOD: USDT via TRC20 network\n"
        f"WALLET ADDRESS: {wallet}\n"
        f"\n"
        f"INSTRUCTIONS:\n"
        f"1. Buy USDT on your preferred exchange (Binance, Coinbase, Kraken, etc.)\n"
        f"2. Send exactly $700 worth of USDT to the TRC20 address above\n"
        f"3. Reply to this email with the transaction hash (TXID)\n"
        f"4. I'll confirm receipt within 1 hour and launch your campaign\n"
        f"\n"
        f"WHY USDT/TRC20:\n"
        f"✅ Instant settlement (1-2 minutes)\n"
        f"✅ No bank fees or delays\n"
        f"✅ No currency conversion needed\n"
        f"\n"
        f"If you have questions about the payment process, just reply here.\n"
        f"\n"
        f"Looking forward to working together!\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    return send_email(email, subject, body)
