"""Timezone inference and recipient-local sending windows."""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SEND_HOUR_START = 8
SEND_HOUR_END = 18
BEST_MORNING = (9, 11)
BEST_AFTERNOON = (14, 16)

DEFAULT_TZ = "America/New_York"

US_STATE_TZ: dict[str, str] = {
    # Pacific
    "CA": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    # Mountain
    "CO": "America/Denver",
    "AZ": "America/Denver",
    "MT": "America/Denver",
    "ID": "America/Denver",
    "WY": "America/Denver",
    "UT": "America/Denver",
    "NM": "America/Denver",
    # Central
    "TX": "America/Chicago",
    "IL": "America/Chicago",
    "MN": "America/Chicago",
    "AL": "America/Chicago",
    "AR": "America/Chicago",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "LA": "America/Chicago",
    "MO": "America/Chicago",
    "MS": "America/Chicago",
    "NE": "America/Chicago",
    "ND": "America/Chicago",
    "OK": "America/Chicago",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "WI": "America/Chicago",
    # Eastern
    "NY": "America/New_York",
    "MA": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "CT": "America/New_York",
    "DE": "America/New_York",
    "DC": "America/New_York",
    "HI": "Pacific/Honolulu",
    "IN": "America/New_York",
    "KY": "America/New_York",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MI": "America/New_York",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NC": "America/New_York",
    "OH": "America/New_York",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WV": "America/New_York",
    "AK": "America/Anchorage",
}

COUNTRY_TZ: dict[str, str] = {
    "UK": "Europe/London",
    "GB": "Europe/London",
    "UNITED KINGDOM": "Europe/London",
    "AU": "Australia/Sydney",
    "AUSTRALIA": "Australia/Sydney",
    "US": DEFAULT_TZ,
    "USA": DEFAULT_TZ,
    "UNITED STATES": DEFAULT_TZ,
}

TZ_ABBREV: dict[str, str] = {
    "America/Los_Angeles": "PT",
    "America/Denver": "MT",
    "America/Chicago": "CT",
    "America/New_York": "ET",
    "Europe/London": "UK",
    "Australia/Sydney": "AEST",
    "Pacific/Honolulu": "HT",
    "America/Anchorage": "AKT",
}

STATE_NAME_TO_CODE: dict[str, str] = {
    "california": "CA",
    "oregon": "OR",
    "washington": "WA",
    "nevada": "NV",
    "colorado": "CO",
    "arizona": "AZ",
    "texas": "TX",
    "illinois": "IL",
    "minnesota": "MN",
    "new york": "NY",
    "massachusetts": "MA",
    "florida": "FL",
    "georgia": "GA",
    "united kingdom": "UK",
    "australia": "AU",
}

_CITY_STATE_RE = re.compile(
    r",\s*([A-Za-z]{2})\s*(?:\d{5}(?:-\d{4})?)?\s*$",
    re.M,
)


def _now_for_tz(tz_name: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(tz_name)
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc).astimezone(tz)
        return now.astimezone(tz)
    override = os.environ.get("SEND_TIME_OVERRIDE", "").strip()
    if override:
        parsed = datetime.fromisoformat(override.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    return datetime.now(tz)


def _parse_state_code(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    upper = text.upper()
    if len(upper) == 2 and upper in US_STATE_TZ:
        return upper
    lower = text.lower()
    if lower in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[lower]
    match = _CITY_STATE_RE.search(text)
    if match:
        code = match.group(1).upper()
        if code in US_STATE_TZ:
            return code
    for code in US_STATE_TZ:
        if re.search(rf"\b{code}\b", upper):
            return code
    for name, code in STATE_NAME_TO_CODE.items():
        if name in lower:
            return code
    return None


def _parse_country(text: str) -> str | None:
    raw = (text or "").strip().upper()
    if not raw:
        return None
    if raw in COUNTRY_TZ:
        return raw
    for key in COUNTRY_TZ:
        if key in raw:
            return key
    return None


def get_recipient_timezone(lead: dict) -> str:
    """
    Infer IANA timezone from lead state/country or text fields.

    Checks explicit fields, then Signal/Company/Notes, then defaults.
    """
    if lead.get("timezone"):
        return str(lead["timezone"])

    for key in ("state", "State"):
        code = _parse_state_code(str(lead.get(key) or ""))
        if code:
            return US_STATE_TZ[code]

    for key in ("country", "Country"):
        country = _parse_country(str(lead.get(key) or ""))
        if country and country in COUNTRY_TZ:
            return COUNTRY_TZ[country]

    blob = " ".join(
        str(lead.get(k) or "")
        for k in ("Signal", "Company", "Notes", "Title", "Contact", "Geo", "city")
    )
    code = _parse_state_code(blob)
    if code:
        return US_STATE_TZ[code]

    country = _parse_country(blob)
    if country and country in COUNTRY_TZ:
        return COUNTRY_TZ[country]

    email = (lead.get("Email") or lead.get("email") or "").lower()
    if email.endswith(".uk"):
        return COUNTRY_TZ["UK"]
    if email.endswith(".au") or email.endswith(".com.au"):
        return COUNTRY_TZ["AU"]

    return DEFAULT_TZ


def is_weekend(local_dt: datetime) -> bool:
    return local_dt.weekday() >= 5


def is_in_send_window(local_dt: datetime) -> bool:
    """Weekday between 08:00 (inclusive) and 18:00 (exclusive)."""
    if is_weekend(local_dt):
        return False
    return SEND_HOUR_START <= local_dt.hour < SEND_HOUR_END


def is_best_send_time(local_dt: datetime) -> bool:
    """Morning 9-11 or afternoon 14-16 local time."""
    if not is_in_send_window(local_dt):
        return False
    hour = local_dt.hour
    return (
        BEST_MORNING[0] <= hour < BEST_MORNING[1]
        or BEST_AFTERNOON[0] <= hour < BEST_AFTERNOON[1]
    )


def can_send_now(tz_name: str, now: datetime | None = None) -> bool:
    local = _now_for_tz(tz_name, now)
    return is_in_send_window(local)


def recipient_local_time(tz_name: str, now: datetime | None = None) -> datetime:
    return _now_for_tz(tz_name, now)


def _candidate_hours() -> list[int]:
    """Preferred send hours: best slots first, then rest of window."""
    best = list(range(BEST_MORNING[0], BEST_MORNING[1]))
    best += list(range(BEST_AFTERNOON[0], BEST_AFTERNOON[1]))
    rest = [
        h
        for h in range(SEND_HOUR_START, SEND_HOUR_END)
        if h not in best
    ]
    return best + rest


def next_send_datetime(tz_name: str, now: datetime | None = None) -> datetime:
    """Next weekday send slot in recipient timezone (prefers 9:00 / 14:00)."""
    tz = ZoneInfo(tz_name)
    local = _now_for_tz(tz_name, now)

    if is_in_send_window(local):
        return local

    for day_offset in range(0, 14):
        day = local.date() + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        for hour in _candidate_hours():
            candidate = datetime.combine(day, time(hour, 0), tzinfo=tz)
            if candidate > local:
                return candidate

    return local + timedelta(days=1)


def tz_abbrev(tz_name: str) -> str:
    return TZ_ABBREV.get(tz_name, tz_name.split("/")[-1])


def format_local_datetime_safe(dt: datetime, tz_name: str) -> str:
    """Cross-platform friendly time formatting."""
    local = dt.astimezone(ZoneInfo(tz_name))
    today = _now_for_tz(tz_name).date()
    label = tz_abbrev(tz_name)
    if local.date() == today:
        day = "today"
    elif local.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = local.strftime("%a %b %d")
    hour = local.hour
    minute = local.minute
    time_str = f"{hour}:{minute:02d}"
    return f"{day} {time_str} {label}"
