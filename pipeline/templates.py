"""Cold email templates (plain text only). 100% async — no calls."""

import os

_DEFAULT_CALENDLY = "https://calendly.com/dentalmeet-agency/new-meeting"
# Kept for env compatibility; cold outreach does NOT book calls.
CALENDLY_URL = (
    os.environ.get("CALENDLY_URL", "").strip() or _DEFAULT_CALENDLY
)

UNSUBSCRIBE_URL = "https://dentalmeet.agency/unsubscribe"
UNSUBSCRIBE_FOOTER = (
    f"To unsubscribe, click here: {UNSUBSCRIBE_URL}\n"
    'Not interested? Reply "unsubscribe" and we\'ll remove you.\n'
)

# Standard agency signature (warm / DentalMeet-owned inbox).
# Cold outreach should later move to a separate sending domain/mailbox.
SIGNATURE = (
    "— Vladislav\n"
    "Founder, DentalMeet\n"
    "hello@dentalmeet.agency\n"
    "\n"
    f"{UNSUBSCRIBE_FOOTER}"
)


def FIRST_EMAIL_TEMPLATE(
    first_name: str,
    company: str,
    opener: str,
    calendly_url: str = CALENDLY_URL,
) -> tuple[str, str]:
    """Return (subject, body) for the first outreach email."""
    name = (first_name or "").strip() or "there"
    company_name = (company or "").strip() or "your agency"
    opener_line = (opener or "").strip()
    _ = calendly_url  # unused — we never ask for a call

    subject = f"meetings with dental practices for {company_name}"

    opener_block = f"{opener_line}\n\n" if opener_line else ""

    body = (
        f"Hi {name},\n"
        f"\n"
        f"{opener_block}"
        f"I run an autonomous B2B lead generation platform that books "
        f"qualified meetings with dental practice owners in 2-3 weeks. "
        f"We handle everything: research, personalization, sending, "
        f"follow-ups, and meeting booking — 100% async, no calls needed.\n"
        f"\n"
        f"Pilot offer (only 3 spots left):\n"
        f"→ $0 upfront — you pay nothing to start\n"
        f"→ $150 per qualified meeting (max $750 for pilot)\n"
        f"→ If we don't deliver 5 meetings, you pay nothing\n"
        f"\n"
        f"What counts as \"qualified\":\n"
        f"✅ Meeting took place (no no-shows)\n"
        f"✅ Attendee is decision-maker (Owner, CEO, Practice Manager)\n"
        f"✅ Duration >= 15 minutes\n"
        f"✅ From your target geography\n"
        f"\n"
        f"Curious if this could work for {company_name}?\n"
        f'Reply "interested" and I\'ll send a short presentation + '
        f"a 5-minute onboarding form.\n"
        f"\n"
        f"{SIGNATURE}"
        f"\n"
        f'P.S. Not relevant? Reply "no thanks" or "unsubscribe" and I\'ll stop.\n'
    )
    return subject, body


def FOLLOWUP_1_TEMPLATE(
    first_name: str,
    company: str = "",
    calendly_url: str = CALENDLY_URL,
) -> tuple[str, str]:
    """Bump email ~3 days later. Returns (subject, body)."""
    name = (first_name or "").strip() or "there"
    company_name = (company or "").strip()
    about = f" for {company_name}" if company_name else ""
    _ = calendly_url

    subject = f"quick bump{about}"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Just bumping this in case it got buried — still curious about "
        f"qualified meetings with dental practice owners?\n"
        f"\n"
        f'Reply "interested" and I\'ll send the short presentation + '
        f"onboarding form (no call needed).\n"
        f"\n"
        f'If not, reply "not interested" and I\'ll close the loop.\n'
        f"\n"
        f"{SIGNATURE}"
    )
    return subject, body


def FOLLOWUP_2_TEMPLATE(
    first_name: str,
    company: str = "",
    calendly_url: str = CALENDLY_URL,
) -> tuple[str, str]:
    """Final note ~7 days later. Returns (subject, body)."""
    name = (first_name or "").strip() or "there"
    company_name = (company or "").strip()
    about = f" / {company_name}" if company_name else ""
    _ = calendly_url

    subject = f"closing the loop{about}"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Last note from me — no pressure either way.\n"
        f"\n"
        f"If booking qualified meetings with dental practices is still on "
        f"your radar, reply \"interested\" and I'll send the presentation + "
        f"form so we can start async.\n"
        f"\n"
        f"Otherwise I'll assume the timing isn't right and won't follow up again.\n"
        f"\n"
        f"{SIGNATURE}"
    )
    return subject, body
