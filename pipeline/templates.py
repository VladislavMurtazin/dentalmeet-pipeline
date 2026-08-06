"""Cold email templates (plain text only)."""

import os

_DEFAULT_CALENDLY = "https://calendly.com/dentalmeet-agency/new-meeting"
CALENDLY_URL = (
    os.environ.get("CALENDLY_URL", "").strip() or _DEFAULT_CALENDLY
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

    subject = f"meetings with dental practices for {company_name}"

    opener_block = f"{opener_line}\n\n" if opener_line else ""

    body = (
        f"Hi {name},\n"
        f"\n"
        f"{opener_block}"
        f"I run an autonomous B2B lead generation platform that books 15-20 "
        f"qualified meetings with dental practice owners in 2-3 weeks. "
        f"We handle everything: research, personalization, sending, "
        f"follow-ups, and meeting booking.\n"
        f"\n"
        f"Pilot offer (only 3 spots left):\n"
        f"→ $0 upfront\n"
        f"→ $150 per booked meeting (or $700 flat)\n"
        f"→ Weekly transparency reports\n"
        f"→ Case-study pricing locked for 6 months\n"
        f"\n"
        f"Worth a quick 15-min call to see if it fits?\n"
        f"\n"
        f"{calendly_url}\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
        f"hello@dentalmeet.agency\n"
        f"dentalmeet.agency\n"
        f"\n"
        f'P.S. Not relevant? Reply "no thanks" and I\'ll stop.\n'
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

    subject = f"quick bump{about}"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Just bumping this in case it got buried — still interested in "
        f"a 15-min look at booking meetings with dental practice owners?\n"
        f"\n"
        f"{calendly_url}\n"
        f"\n"
        f'If not, reply "not interested" and I\'ll close the loop.\n'
        f"\n"
        f"— Vladislav\n"
        f"DentalMeet\n"
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

    subject = f"closing the loop{about}"
    body = (
        f"Hi {name},\n"
        f"\n"
        f"Last note from me — no pressure either way.\n"
        f"\n"
        f"If booking qualified meetings with dental practices is still on "
        f"your radar, happy to show how the pilot works in 15 minutes:\n"
        f"\n"
        f"{calendly_url}\n"
        f"\n"
        f"Otherwise I'll assume the timing isn't right and won't follow up again.\n"
        f"\n"
        f"— Vladislav\n"
        f"Founder, DentalMeet\n"
    )
    return subject, body
