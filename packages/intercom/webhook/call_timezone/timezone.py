"""Infer IANA timezone from an E.164 phone number using Google's libphonenumber."""

from datetime import datetime, timezone as dt_timezone

import phonenumbers
from phonenumbers import geocoder as pn_geocoder
from phonenumbers import timezone as pn_timezone
from phonenumbers.phonenumberutil import NumberParseException


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
        from zoneinfo import ZoneInfo
        tz_obj = ZoneInfo(tz_name)
        local_now = now_utc.astimezone(tz_obj)
        utc_offset_seconds = local_now.utcoffset().total_seconds()
        offset_hours = int(utc_offset_seconds // 3600)
        offset_mins = int(abs(utc_offset_seconds) % 3600 // 60)
        if offset_mins:
            utc_offset = f"UTC{offset_hours:+d}:{offset_mins:02d}"
        else:
            utc_offset = f"UTC{offset_hours:+d}"
        local_time = local_now.strftime("%-I:%M %p %Z")
    except Exception:
        utc_offset = None
        local_time = None

    return {
        "timezone": tz_name,
        "country": country,
        "location": location,
        "area_code": area_code,
        "confidence": confidence,
        "utc_offset": utc_offset,
        "local_time": local_time,
        "all_zones": list(zones) if len(zones) > 1 else None,
    }
