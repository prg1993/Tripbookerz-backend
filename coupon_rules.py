"""
Coupon rules (Stage 3). Pure functions only: no database, no Flask, so the
rules can be tested on their own and changed in one place.

Owner-approved rules (see STAFF_PORTAL_STAGES_2_3_4_DESIGN.md):
  * A discount may only come out of Tripbookerz's own fees: the platform fee
    plus the convenience fee. It can never reduce the airline/hotel fare, nor
    the agent's own markup (that is the agent's money).
      fee_ceiling = platform_fee + convenience_fee
    (Owner decision 8 Nov 2026; replaces the earlier ceiling that included 10%
    of the agent fee.) GST is worked out AFTER the discount, on what remains.
  * Sales Manager: up to COUPON_MANAGER_LIMIT rupees discount per booking
    (default 1000) and total exposure (per-booking discount x max uses) up
    to COUPON_MANAGER_TOTAL_CAP (default 25000). Beyond that -> Admin approval.
  * Going above the fee ceiling needs an Admin's case-by-case approval with
    a written reason. A Sales Manager can only ASK; never self-approve.
  * Admin-created coupons are active immediately (still audited).
"""
import os
from datetime import date, datetime, timedelta, timezone

CODE_MIN, CODE_MAX = 3, 30


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def limits():
    """Read on every call so a Render env change needs only a restart."""
    return {
        "manager_limit": _int_env("COUPON_MANAGER_LIMIT", 1000),
        "manager_total_cap": _int_env("COUPON_MANAGER_TOTAL_CAP", 25000),
    }


def fee_ceiling(platform_fee, convenience_fee):
    """Rupees of Tripbookerz's own fees on this booking."""
    return int(platform_fee) + int(convenience_fee)


def compute_discount(ctype, value, max_discount, allow_over_ceiling,
                     net, platform_fee, convenience_fee, agent_fee):
    """Discount in whole rupees for one booking. Never negative, never more
    than the booking subtotal; never more than the fee ceiling unless an
    Admin approved the override on this coupon."""
    subtotal = int(net) + int(platform_fee) + int(convenience_fee) + int(agent_fee)
    ceiling = fee_ceiling(platform_fee, convenience_fee)
    base = subtotal if allow_over_ceiling else ceiling
    if ctype == "percent":
        d = round(base * float(value) / 100)
    else:
        d = round(float(value))
    if max_discount:
        d = min(d, int(max_discount))
    d = min(d, base, subtotal)
    return max(0, int(d))


def per_booking_max(ctype, value, max_discount):
    """Largest rupee discount one booking can get, known at creation time.
    None = not bounded in rupees (percent without a rupee cap)."""
    if ctype == "flat":
        return round(float(value))
    if max_discount:
        return int(max_discount)
    return None


def validate_new(body):
    """Returns (clean_dict, error). Strict: unknown/odd input is rejected."""
    code = str(body.get("code") or "").strip().upper()
    if not (CODE_MIN <= len(code) <= CODE_MAX) or not all(ch.isalnum() or ch in "-_" for ch in code):
        return None, f"Code must be {CODE_MIN}-{CODE_MAX} characters: letters, numbers, - or _"
    ctype = body.get("type")
    if ctype not in ("percent", "flat"):
        return None, "type must be 'percent' or 'flat'"
    try:
        value = float(body.get("value"))
    except (TypeError, ValueError):
        return None, "value must be a number"
    if value <= 0 or (ctype == "percent" and value > 100):
        return None, "value must be above 0 (and at most 100 for percent)"
    md = body.get("maxDiscount")
    if md in (None, "", 0):
        md = None
    else:
        try:
            md = int(md)
        except (TypeError, ValueError):
            return None, "maxDiscount must be a whole number of rupees"
        if md <= 0:
            return None, "maxDiscount must be above 0"
    try:
        max_uses = int(body.get("max", 1))
    except (TypeError, ValueError):
        return None, "max (number of uses) must be a whole number"
    if max_uses < 1 or max_uses > 1_000_000:
        return None, "max (number of uses) must be between 1 and 1,000,000"
    try:
        vf = date.fromisoformat(str(body.get("from")))
        vt = date.fromisoformat(str(body.get("to")))
    except ValueError:
        return None, "from / to must be dates in YYYY-MM-DD format"
    if vt < vf:
        return None, "The end date is before the start date"
    override = bool(body.get("overrideCeiling"))
    reason = str(body.get("reason") or "").strip()[:500]
    kind = body.get("kind", "standard")
    if kind not in ("standard", "campaign"):
        return None, "kind must be 'standard' or 'campaign'"
    campaign_id = body.get("campaignId")
    if kind == "campaign":
        if len(reason) < 10:
            return None, "A reason (at least 10 characters) is required for a campaign offer"
        try:
            campaign_id = int(campaign_id)
        except (TypeError, ValueError):
            return None, "Choose the campaign this offer belongs to"
    else:
        campaign_id = None
    if override:
        if len(reason) < 10:
            return None, "A reason (at least 10 characters) is required to go above the fee ceiling"
        if per_booking_max(ctype, value, md) is None:
            return None, "Above-ceiling coupons need a rupee limit: use a flat amount or set a maximum discount"
    return {
        "code": code, "type": ctype, "value": value, "max_discount": md,
        "max_uses": max_uses, "valid_from": vf.isoformat(), "valid_to": vt.isoformat(),
        "override": override, "reason": reason, "kind": kind, "campaign_id": campaign_id,
    }, None


def decide_status(clean, is_admin, lim=None):
    """-> (status, reasons). status: 'active' or 'pending_approval'."""
    lim = lim or limits()
    if is_admin:
        return "active", []
    reasons = []
    if clean.get("kind") == "campaign":
        reasons.append("Campaign offers always need Admin approval")
    pb = per_booking_max(clean["type"], clean["value"], clean["max_discount"])
    if pb is None:
        reasons.append("Percent coupon without a maximum discount in rupees")
    elif pb > lim["manager_limit"]:
        reasons.append(f"Discount per booking (Rs {pb}) is above the Rs {lim['manager_limit']} manager limit")
    if pb is not None and pb * clean["max_uses"] > lim["manager_total_cap"]:
        reasons.append(f"Total exposure (Rs {pb * clean['max_uses']}) is above the Rs {lim['manager_total_cap']} cap")
    if clean["override"]:
        reasons.append("Requests a discount above the fee ceiling")
    return ("pending_approval" if reasons else "active"), reasons


def today_local(now_utc=None):
    """Today's date (YYYY-MM-DD) in the business time zone, so a coupon valid
    'to 30 Nov' runs until midnight India time, not 5:30 am. Zone is set by
    COUPON_TIMEZONE (default Asia/Kolkata)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = now_utc.astimezone(ZoneInfo(os.environ.get("COUPON_TIMEZONE", "Asia/Kolkata")))
    except Exception:
        local = now_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return local.strftime("%Y-%m-%d")


def usable(status):
    """NULL status = coupon created before Stage 3 -> treated as active."""
    return status in (None, "active")
