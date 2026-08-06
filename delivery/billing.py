"""Billing helpers: $150/meeting or $700 flat pilot."""

from __future__ import annotations

import os

from pipeline.to_sheets import connect

RATE_PER_MEETING = 150
FLAT_PILOT = 700


def _client_pricing(client_email: str) -> str:
    try:
        gc = connect()
        sh = gc.open(os.environ.get("SHEET_NAME", "Pilot CRM"))
        ws = sh.worksheet("Clients")
        values = ws.get_all_values()
        headers = [h.strip().lower() for h in values[0]]
        email_i = headers.index("email") if "email" in headers else 2
        price_i = headers.index("pricing") if "pricing" in headers else None
        for row in values[1:]:
            if len(row) > email_i and row[email_i].strip().lower() == client_email.lower():
                if price_i is not None and price_i < len(row):
                    return (row[price_i] or "").lower()
    except Exception:
        pass
    return ""


def compute_amount(client_email: str, meetings: int) -> tuple[float, str]:
    """Return (amount_usd, model_label)."""
    pricing = _client_pricing(client_email)
    if "700" in pricing or "flat" in pricing:
        return float(FLAT_PILOT), "flat_700"
    if "150" in pricing or "per" in pricing or "meeting" in pricing:
        return float(meetings * RATE_PER_MEETING), "per_meeting_150"
    # default: per meeting, but floor to flat if they chose pilot vaguely
    if meetings <= 0:
        return 0.0, "per_meeting_150"
    return float(meetings * RATE_PER_MEETING), "per_meeting_150"


def invoice_summary(client_email: str, meetings: int) -> dict:
    amount, model = compute_amount(client_email, meetings)
    return {
        "client_email": client_email,
        "meetings": meetings,
        "amount": amount,
        "model": model,
        "wallet": os.environ.get("TRC20_WALLET_ADDRESS", ""),
    }
