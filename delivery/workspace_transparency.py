"""Google Workspace access transparency — scopes, emails, Plan B messaging."""

from __future__ import annotations

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]

SCOPE_DETAILS: list[dict[str, str]] = [
    {
        "scope": "https://www.googleapis.com/auth/gmail.send",
        "label": "Send email on your behalf",
        "description": "Send outreach emails from your domain with your signature.",
        "accessed": "yes",
    },
    {
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "label": "Read email",
        "description": "Read replies to campaign emails so we can track responses and forward them to you.",
        "accessed": "yes",
    },
    {
        "scope": "https://www.googleapis.com/auth/calendar",
        "label": "View calendars",
        "description": "Optional — used only if you connect Calendly via Google Calendar for meeting tracking.",
        "accessed": "optional",
    },
]

WORKSPACE_INFO_URL = "https://dentalmeet.agency/workspace-info"


def get_access_scopes() -> list[dict[str, str]]:
    """Return requested OAuth scopes with human-readable labels."""
    return [dict(s) for s in SCOPE_DETAILS]


def _first_name(client: dict) -> str:
    raw = (client.get("agency_owner_name") or client.get("name") or "").strip()
    if raw:
        return raw.split()[0]
    return "there"


def generate_transparency_email(client: dict) -> tuple[str, str]:
    """
    OAuth connect email with clear scope explanation and Plan B alternative.

    client keys: agency_owner_name (or name), client_email, oauth_url
    """
    name = _first_name(client)
    email = (client.get("client_email") or "").strip()
    oauth_url = (client.get("oauth_url") or "").strip()
    domain = email.split("@")[-1] if "@" in email else "your domain"

    subject = "Connect your Google Workspace to launch your campaign"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"To launch your dental outreach campaign, we need to connect your "
        f"Google Workspace. This lets us send emails to dental practices "
        f"on your behalf — from YOUR domain ({domain}), with YOUR signature.\n"
        f"\n"
        f"🔒 WHAT WE ACCESS (and nothing more):\n"
        f"✅ Send emails from your domain\n"
        f"✅ Read replies (so we can track responses for you)\n"
        f"\n"
        f"❌ What we NEVER access:\n"
        f"- Your files, documents, or Google Drive\n"
        f"- Your contacts or calendar (unless you opt in)\n"
        f"- Your password\n"
        f"- Any personal emails outside this campaign\n"
        f"\n"
        f"🛡️ YOUR CONTROL:\n"
        f"- You can revoke access anytime in Google Account → Security → "
        f"Third-party access\n"
        f"- All sent emails appear in YOUR Sent folder\n"
        f"- All replies arrive directly in YOUR inbox\n"
        f"- We never see your password\n"
        f"\n"
        f"Full details: {WORKSPACE_INFO_URL}\n"
        f"\n"
        f"⏱️ How it works (2 minutes):\n"
        f"1. Click this secure link: {oauth_url}\n"
        f"2. Sign in with your Google Workspace account ({email})\n"
        f'3. Click "Allow" when prompted\n'
        f"\n"
        f"🤝 PREFER NOT TO CONNECT?\n"
        f"No problem! We have an alternative: we can send emails on your "
        f"behalf from our own accounts, signed with your name and company. "
        f'Just reply "Plan B" and we\'ll set that up instead.\n'
        f"\n"
        f"Questions? Just reply here — happy to clarify anything.\n"
        f"\n"
        f"Best,\n"
        f"Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    return subject, body


def generate_plan_b_confirmation(client: dict) -> tuple[str, str]:
    """Confirmation email when client chooses Plan B (no Workspace access)."""
    name = _first_name(client)
    company = (client.get("company") or client.get("client_company") or "your company").strip()

    subject = "You're all set — Plan B (no Workspace connection needed)"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Great! We'll send from our accounts on your behalf, signed as "
        f"{company}. We'll forward all replies to you.\n"
        f"\n"
        f"How Plan B works:\n"
        f"✅ Emails go out from DentalMeet accounts with your name and company in the signature\n"
        f"✅ Replies are forwarded to your inbox with a [DentalMeet] prefix\n"
        f"✅ No Google Workspace connection required\n"
        f"✅ You can switch to Workspace sending anytime later for higher deliverability\n"
        f"\n"
        f"Your campaign can start as soon as we finish building your dental practice list.\n"
        f"\n"
        f"Questions? Just reply here.\n"
        f"\n"
        f"Best,\n"
        f"Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    return subject, body


def send_plan_b_confirmation(
    client_email: str,
    *,
    name: str = "",
    company: str = "",
    from_email: str | None = None,
) -> bool:
    """Send Plan B setup confirmation to client."""
    from pipeline.email_sender import send_email
    from pipeline.to_sheets import is_email

    email = client_email.strip().lower()
    if not is_email(email):
        return False
    subject, body = generate_plan_b_confirmation(
        {"agency_owner_name": name, "client_email": email, "company": company}
    )
    sender = (from_email or "").strip().lower() or None
    if not sender:
        try:
            from pipeline.conversation_owner import resolve_from_email

            sender = resolve_from_email(email)
        except Exception:
            sender = None
    return send_email(email, subject, body, from_email=sender)
