---
name: Drop phonenumbers dependency
overview: Replace the `phonenumbers` library with pure-Python lookup tables for phone-to-timezone inference, eliminating the heavy dependency that causes 8-10s cold starts.
todos:
  - id: create-branch
    content: Create feature branch from main
    status: pending
  - id: area-codes
    content: Create area_codes.py with ~370 NANP area code to IANA timezone mappings
    status: pending
  - id: country-codes
    content: Create country_codes.py with ~200 international calling code to timezone mappings
    status: pending
  - id: rewrite-timezone
    content: Rewrite timezone.py using lookup tables instead of phonenumbers
    status: pending
  - id: update-deps
    content: Remove phonenumbers from requirements.txt, set memory back to 256MB
    status: pending
  - id: update-tests
    content: Update timezone tests to not depend on phonenumbers
    status: pending
  - id: deploy-test
    content: Deploy, test cold start time and Intercom validation
    status: pending
isProject: false
---

# Drop phonenumbers Dependency

## Approach

Replace [packages/intercom/webhook/call_timezone/timezone.py](packages/intercom/webhook/call_timezone/timezone.py) with a self-contained implementation using two lookup tables:

### 1. NANP area code table (~370 entries)

Covers US, Canada, and Caribbean (`+1` numbers). Maps 3-digit area code to IANA timezone. This is the bulk of your call volume and has **high confidence** -- area codes are assigned to specific geographic regions with known timezones. Data sourced from NANPA (public, stable).

```python
NANP_AREA_CODE_TZ = {
    "201": "America/New_York",    # NJ
    "202": "America/New_York",    # DC
    "205": "America/Chicago",     # AL
    "206": "America/Los_Angeles", # WA - Seattle
    "212": "America/New_York",    # NY - Manhattan
    "303": "America/Denver",      # CO - Denver
    # ... ~370 entries total
}
```

### 2. Country calling code table (~200 entries)

Maps international calling codes (e.g., `44` -> UK, `49` -> Germany) to their primary IANA timezone. For single-timezone countries (UK, Germany, Japan, etc.) this is perfect. For multi-timezone countries (Russia, Australia, Brazil), it picks the most populous timezone and marks confidence as "approximate".

```python
COUNTRY_CODE_TZ = {
    "7":   ("RU", "Europe/Moscow"),        # Russia (approximate)
    "20":  ("EG", "Africa/Cairo"),
    "27":  ("ZA", "Africa/Johannesburg"),
    "33":  ("FR", "Europe/Paris"),
    "44":  ("GB", "Europe/London"),
    "49":  ("DE", "Europe/Berlin"),
    "61":  ("AU", "Australia/Sydney"),      # Australia (approximate)
    "81":  ("JP", "Asia/Tokyo"),
    "86":  ("CN", "Asia/Shanghai"),
    "91":  ("IN", "Asia/Kolkata"),
    # ... ~200 entries total
}
```

### Phone number parsing

Simple string parsing of E.164 format -- no library needed:
- Strip `+` prefix
- Try matching country code (1-3 digits, longest match first)
- For `+1` numbers, extract the 3-digit area code and look up in NANP table
- For others, look up country code table

### What stays the same

- The `infer_timezone()` function signature and return dict shape are unchanged
- `handler.py` and `__main__.py` remain untouched
- UTC offset and local time calculation uses stdlib `zoneinfo` (already in use)
- Tests updated to not depend on phonenumbers

### Coverage quality

| Caller origin | Accuracy | Notes |
|---|---|---|
| US/CA (area code known) | Exact | ~370 area codes mapped to specific IANA timezone |
| Single-tz countries (UK, DE, JP, FR, etc.) | Exact | ~140 countries with one timezone |
| Multi-tz countries (RU, AU, BR, MX, etc.) | Approximate | Falls back to most populous timezone, marked as "approximate" |
| Invalid/unknown numbers | Returns None | Same as current behavior |

### What changes in the repo

- Rewrite `timezone.py` with lookup tables (no external imports except stdlib `zoneinfo`)
- Create `area_codes.py` for the NANP mapping (keeps `timezone.py` readable)
- Create `country_codes.py` for the international mapping
- Remove `phonenumbers` from `requirements.txt`
- Update tests to remove `phonenumbers` assertions
- Memory limit can go back down to 256MB
