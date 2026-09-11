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
from .templates import signature_for
from .to_sheets import is_email

ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = ROOT / "data" / "drafts"
SUPPRESSION_FILE = ROOT / "data" / "suppression.txt"
DNC_FILE = ROOT / "data" / "do_not_contact.txt"

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
- "What if I don't get meetings?" -> "Pilot: $0 upfront, $150/qualified meeting
  (max $750). No qualified meetings = $0. You only pay after the pilot."
- "Can I see email examples?" -> "Sure! Here are 3 examples from recent campaigns."
- "How long until results?" -> "First meetings typically book in 7-10 days."
- "Do you offer payment methods besides crypto?" ->
  "For the pilot, we accept USDT (TRC20) for instant settlement.
  Wire transfer is possible but adds $50-100 in fees."
- "Can we do a call?" -> "We work 100% async — no calls.
  Reply interested and I'll send the presentation + onboarding form."

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
    """Categorize inbound message for routing (multilingual keyword classifier)."""
    from .reply_classifier import classify_reply

    return classify_reply(email_body).get("category") or "other"


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


def add_to_do_not_contact(email_addr: str) -> None:
    """Permanent do-not-contact after repeated declines; also suppress."""
    email_addr = (email_addr or "").strip().lower()
    if not is_email(email_addr):
        return
    DNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if DNC_FILE.exists():
        existing = {
            line.strip().lower()
            for line in DNC_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    if email_addr not in existing:
        with DNC_FILE.open("a", encoding="utf-8") as f:
            f.write(email_addr + "\n")
        print(f"Added to do_not_contact after 2nd decline: {email_addr}")
    add_to_suppression(email_addr)


def is_do_not_contact(email_addr: str) -> bool:
    email_addr = (email_addr or "").strip().lower()
    if not DNC_FILE.exists():
        return False
    return email_addr in {
        line.strip().lower()
        for line in DNC_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def is_suppressed(email_addr: str) -> bool:
    email_addr = (email_addr or "").strip().lower()
    if is_do_not_contact(email_addr):
        return True
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
    if "contact" in low or "apollo" in low or "find" in low:
        return (
            "We use Apollo.io + custom enrichment, targeting practice owners "
            "and decision-makers only. Happy to share more detail if useful."
        )
    if "pay" in low or "meeting" in low and "don't" in low:
        return (
            "You only pay for qualified meetings (decision-maker, your geo, "
            "15+ min, actually held). No results = no payment. "
            "Happy to walk through the pilot pricing if you want."
        )
    if "call" in low or "zoom" in low or "meet" in low:
        return (
            "We work 100% async — no calls on our side. "
            'Reply "interested" and I\'ll send the short presentation + '
            "onboarding form so we can start."
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


def send_upfront_payment_instructions(
    lead_email: str,
    first_name: str,
    amount: int = 300,
    *,
    from_email: str | None = None,
) -> bool:
    """Send USDT TRC20 details for Repeat setup fee (or flat)."""
    email = (lead_email or "").strip()
    if not is_email(email):
        print(f"send_upfront_payment_instructions: bad email {email!r}")
        return False
    wallet = _wallet()
    if not wallet:
        print("send_upfront_payment_instructions: TRC20_WALLET_ADDRESS missing")
        return False

    name = (first_name or "").strip() or "there"
    subject = "payment details — setup fee"
    sender = (from_email or "").strip().lower() or None
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Here are the payment details for your setup fee:\n"
        f"\n"
        f"AMOUNT: ${amount} USD\n"
        f"PAYMENT METHOD: USDT via TRC20 network\n"
        f"WALLET ADDRESS: {wallet}\n"
        f"\n"
        f"INSTRUCTIONS:\n"
        f"1. Send ${amount} USDT (TRC20) to the address above\n"
        f"2. Reply with the transaction hash (TXID)\n"
        f"3. I'll confirm within ~1 hour and launch\n"
        f"\n"
        f"{signature_for(sender)}"
    )
    return send_email(
        email,
        subject,
        body,
        from_email=sender,
    )


def send_payment_instructions(
    lead_email: str,
    first_name: str,
    *,
    from_email: str | None = None,
) -> bool:
    """
    Payment email based on client type.
    Pilot → remind post-pay model (no charge now).
    Repeat → $300 upfront instructions.
    """
    email = (lead_email or "").strip()
    if not is_email(email):
        print(f"send_payment_instructions: bad email {email!r}")
        return False

    name = (first_name or "").strip() or "there"
    sender = (from_email or "").strip().lower() or None
    try:
        from delivery.client_tracker import get_client_payment_model

        model = get_client_payment_model(email)
    except Exception:
        model = {"client_type": "Pilot", "upfront": 0}

    if (model.get("client_type") or "Pilot").lower() == "repeat":
        amt = int(model.get("upfront") or 300)
        return send_upfront_payment_instructions(
            email, name, amount=amt, from_email=sender
        )

    # Pilot: no upfront — explain post-payment
    subject = "payment model — pilot (pay after)"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"For the pilot there is $0 upfront.\n"
        f"\n"
        f"After the pilot we invoice $150 × qualified meetings "
        f"(max $750). You'll get a full qualification breakdown.\n"
        f"\n"
        f"Pay later via USDT TRC20 — I'll send the exact amount + wallet "
        f"when the pilot wraps.\n"
        f"\n"
        f"{signature_for(sender)}"
    )
    return send_email(email, subject, body, from_email=sender)
