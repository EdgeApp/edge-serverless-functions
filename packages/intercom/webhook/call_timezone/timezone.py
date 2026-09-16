"""Infer IANA timezone from an E.164 phone number using Google's libphonenumber."""

from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers import geocoder as pn_geocoder
from phonenumbers import timezone as pn_timezone
from phonenumbers.phonenumberutil import NumberParseException


SAN_DIEGO_TIMEZONE = "America/Los_Angeles"
ROME_TIMEZONE = "Europe/Rome"


def _format_utc_offset(offset) -> str:
    """Format a UTC offset without losing negative fractional hours."""
    total_minutes = int(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    suffix = f":{minutes:02d}" if minutes else ""
    return f"UTC{sign}{hours}{suffix}"


def _timezone_difference_minutes(
    caller_timezone: str, reference_timezone: str, at_utc: datetime
) -> int:
    """Return caller wall-clock minutes ahead of a reference timezone."""
    caller_offset = at_utc.astimezone(ZoneInfo(caller_timezone)).utcoffset()
    reference_offset = at_utc.astimezone(ZoneInfo(reference_timezone)).utcoffset()
    return int((caller_offset - reference_offset).total_seconds() / 60)


def infer_timezone(phone_e164: str) -> dict:
    """Return timezone info for an E.164 phone string.

    Returns a dict with keys:
        timezone   - IANA timezone string (e.g. "America/New_York")
        country    - ISO 3166-1 alpha-2 code (e.g. "US")
        location   - human-readable location (e.g. "New York, NY")
        area_code  - 3-digit area code for NANP numbers, else None
        confidence - "high" (single match) or "approximate" (picked from multiple)
        utc_offset - e.g. "UTC-5"
        local_time - local time at the moment of inference
        difference_from_san_diego_minutes - caller wall-clock delta from San Diego
        difference_from_rome_minutes - caller wall-clock delta from Rome
    """
    try:
        parsed = phonenumbers.parse(phone_e164, None)
    except NumberParseException:
        return None
    country = phonenumbers.region_code_for_number(parsed)

    area_code = None
    national = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    if country in ("US", "CA"):
        digits = "".join(c for c in national if c.isdigit())
        if len(digits) >= 10:
            area_code = digits[:3]

    zones = pn_timezone.time_zones_for_number(parsed)

    if not zones:
        return None

    if len(zones) == 1:
        tz_name = zones[0]
        confidence = "high"
    else:
        tz_name = zones[0]
        confidence = "approximate"

    location = pn_geocoder.description_for_number(parsed, "en") or None

    now_utc = datetime.now(dt_timezone.utc)
    try:
        tz_obj = ZoneInfo(tz_name)
        local_now = now_utc.astimezone(tz_obj)
        utc_offset = _format_utc_offset(local_now.utcoffset())
        local_time = local_now.strftime("%-I:%M %p %Z")
        difference_from_san_diego_minutes = _timezone_difference_minutes(
            tz_name, SAN_DIEGO_TIMEZONE, now_utc
        )
        difference_from_rome_minutes = _timezone_difference_minutes(
            tz_name, ROME_TIMEZONE, now_utc
        )
    except Exception:
        utc_offset = None
        local_time = None
        difference_from_san_diego_minutes = None
        difference_from_rome_minutes = None

    return {
        "timezone": tz_name,
        "country": country,
        "location": location,
        "area_code": area_code,
        "confidence": confidence,
        "utc_offset": utc_offset,
        "local_time": local_time,
        "difference_from_san_diego_minutes": difference_from_san_diego_minutes,
        "difference_from_rome_minutes": difference_from_rome_minutes,
        "all_zones": list(zones) if len(zones) > 1 else None,
    }
