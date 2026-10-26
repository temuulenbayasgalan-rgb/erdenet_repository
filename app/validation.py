"""Input rules shared by web forms and command-line tools."""
import re
from datetime import datetime, timedelta, timezone

# Mongolia is UTC+8 all year (no daylight saving). A fixed offset avoids needing
# the tzdata package on Windows.
OFFICE_TZ = timezone(timedelta(hours=8))


def office_today():
    """Today's date in Erdenet, regardless of the server's own clock."""
    return datetime.now(OFFICE_TZ).date()

# Wage per worker for one shift, whole tögrög.
MIN_WAGE = 1_000
MAX_WAGE = 5_000_000
WAGE_RANGE_MESSAGE = "Цалин 1 000-5 000 000 ₮ байх ёстой."

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 100

_PHONE_RE = re.compile(r"\d{8}")
_PHONE_SEPARATORS_RE = re.compile(r"[\s\-()]")


def normalize_phone(value):
    """Turn '+976 9911-2233' into '99112233'. Leaves other input for validation to reject."""
    if not value:
        return value
    phone = _PHONE_SEPARATORS_RE.sub("", value)
    if phone.startswith("+976"):
        phone = phone[4:]
    return phone


def is_valid_phone(value):
    """Mongolian phone numbers are 8 digits."""
    return bool(value) and _PHONE_RE.fullmatch(value) is not None


def strip_text(value):
    return value.strip() if isinstance(value, str) else value
