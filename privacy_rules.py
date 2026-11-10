"""
Privacy / retention rules. Pure functions only (no database, no Flask) so they
can be tested alone and changed in one place.

Owner-approved starting values (8-9 Nov 2026, to be confirmed by the owner's CA
and lawyer before launch). All are settings (Render environment), not code:
  RETAIN_ATTEMPTS_DAYS     180   abandoned booking attempts (name/phone/email)
  RETAIN_CALLBACKS_DAYS    180   callback requests (phone)
  BOOKING_ANONYMISE_DAYS   2920  name/phone/email removed from a booking after
                                 8 years. The money record (amounts, GST, IDs)
                                 stays for tax and dispute purposes.
  SESSION_PURGE_DAYS       30    expired staff sessions / reset tokens (they hold IPs)
"""
import os
from datetime import date, datetime, timedelta

# Keys that can carry personal data inside a booking's free-form `details` JSON.
PII_DETAIL_KEYS = {"guests", "passengers", "travelers", "travellers", "traveler", "traveller",
                   "contact", "name", "phone", "email", "mobile"}
OPEN_REFUND_STATES = {"requested", "processing"}


def _int_env(name, default):
    try:
        v = int(os.environ.get(name, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def settings():
    """Read on every call so a Render env change needs only a restart."""
    return {
        "attempts_days": _int_env("RETAIN_ATTEMPTS_DAYS", 180),
        "callbacks_days": _int_env("RETAIN_CALLBACKS_DAYS", 180),
        "booking_anonymise_days": _int_env("BOOKING_ANONYMISE_DAYS", 2920),
        "session_purge_days": _int_env("SESSION_PURGE_DAYS", 30),
    }


def cutoff(days, now=None):
    return (now or datetime.utcnow()) - timedelta(days=days)


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def travel_end_date(kind, details):
    """Last day of the trip, or None when it cannot be worked out. None is
    treated as 'trip not known to be finished' by the erasure tool."""
    details = details if isinstance(details, dict) else {}
    if kind == "flight":
        return _d(details.get("date"))
    if kind == "hotel":
        ci = _d(details.get("checkin"))
        try:
            nights = int(details.get("nights"))
        except (TypeError, ValueError):
            return None
        return ci + timedelta(days=nights) if ci and nights >= 0 else None
    return None


def strip_pii(details):
    """Copy of `details` without the keys that can hold personal data."""
    if not isinstance(details, dict):
        return details
    return {k: v for k, v in details.items() if str(k).lower() not in PII_DETAIL_KEYS}


def phone_tail(value):
    """Last 10 digits of a phone number, so '+91 98765 43210' and '9876543210' match."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def classify_identifier(value):
    """-> ('phone', last10) | ('email', lowercase) | (None, None)"""
    v = str(value or "").strip()
    if "@" in v and "." in v.split("@")[-1] and len(v) <= 255:
        return "email", v.lower()
    t = phone_tail(v)
    return ("phone", t) if t else (None, None)


def booking_skip_reason(legal_hold, refund_status, kind, details, today=None):
    """Why a booking's personal data must NOT be removed right now (None = may be removed)."""
    if legal_hold:
        return "legal_hold"
    if (refund_status or "none") in OPEN_REFUND_STATES:
        return "refund_open"
    end = travel_end_date(kind, details)
    if end is None:
        return "travel_date_unknown"
    if end >= (today or date.today()):
        return "trip_not_finished"
    return None
