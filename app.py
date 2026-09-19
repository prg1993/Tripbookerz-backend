"""
Tripbookerz backend — MSG91 OTP relay + persistent booking/coupon/campaign
data store (Postgres, e.g. Neon).

This is the piece that MUST run on a real server, never in the browser.
The MSG91 auth key and the database URL are both secrets: if either ends
up in frontend JavaScript, anyone viewing the page source can use them
directly (send OTPs on your bill, or read/write your database).

OTP endpoints:
    POST /api/send-otp    { "mobile": "9545555638" }
    POST /api/verify-otp  { "mobile": "9545555638", "otp": "123456" }
    POST /api/resend-otp  { "mobile": "9545555638" }

Booking/data endpoints (backed by Postgres, not localStorage):
    POST   /api/bookings            create a booking
    GET    /api/bookings            list bookings (?role=agent to filter)
    GET    /api/bookings/<id>       look up by Tripbookerz Booking ID OR partner ref
    POST   /api/coupons             create a coupon
    GET    /api/coupons             list coupons
    DELETE /api/coupons/<code>      delete a coupon
    POST   /api/coupons/<code>/use  increment usage count (on redemption)
    POST   /api/campaigns           create a campaign
    GET    /api/campaigns           list campaigns
    PATCH  /api/campaigns/<id>      toggle active/paused
    GET    /api/markup-config       get current platform markup
    PUT    /api/markup-config       set platform markup
"""

import os
import http.client
import json
from urllib.parse import urlencode
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS

from models import make_session, Booking, Coupon, Campaign, MarkupConfig

app = Flask(__name__)

# In production, lock this down to your real domain instead of "*".
# e.g. CORS(app, origins=["https://www.tripbookerz.com"])
CORS(app)

MSG91_AUTH_KEY = os.environ.get("MSG91_AUTH_KEY")
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID")
MSG91_HOST = "control.msg91.com"

DATABASE_URL = os.environ.get("DATABASE_URL")
Session = None
if DATABASE_URL:
    Session = make_session(DATABASE_URL)
else:
    print(
        "WARNING: DATABASE_URL is not set. Booking/coupon/campaign endpoints "
        "will fail until it's configured. See .env.example."
    )

if not MSG91_AUTH_KEY or not MSG91_TEMPLATE_ID:
    print(
        "WARNING: MSG91_AUTH_KEY / MSG91_TEMPLATE_ID are not set. "
        "Set them as environment variables before sending real OTPs. "
        "See .env.example."
    )


def msg91_request(method: str, path: str, params: dict):
    """Make a single request to MSG91 and return the parsed JSON response."""
    conn = http.client.HTTPSConnection(MSG91_HOST)
    query = urlencode(params)
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
    }
    conn.request(method, f"{path}?{query}", headers=headers)
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    conn.close()
    try:
        return res.status, json.loads(raw)
    except ValueError:
        # MSG91 occasionally returns a plain string instead of JSON
        return res.status, {"message": raw}


def normalize_mobile(mobile: str) -> str:
    """Ensure the number carries the 91 country code, as MSG91 expects."""
    mobile = "".join(ch for ch in mobile if ch.isdigit())
    if len(mobile) == 10:
        return "91" + mobile
    return mobile


@app.route("/api/send-otp", methods=["POST"])
def send_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    if not mobile:
        return jsonify(success=False, error="mobile is required"), 400
    if not MSG91_AUTH_KEY or not MSG91_TEMPLATE_ID:
        return jsonify(success=False, error="Server OTP config missing"), 500

    params = {
        "template_id": MSG91_TEMPLATE_ID,
        "mobile": normalize_mobile(mobile),
        "authkey": MSG91_AUTH_KEY,
    }
    status, data = msg91_request("POST", "/api/v5/otp", params)
    ok = status == 200 and data.get("type") == "success"
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    otp = body.get("otp", "")
    if not mobile or not otp:
        return jsonify(success=False, error="mobile and otp are required"), 400
    if not MSG91_AUTH_KEY:
        return jsonify(success=False, error="Server OTP config missing"), 500

    params = {
        "mobile": normalize_mobile(mobile),
        "otp": otp,
        "authkey": MSG91_AUTH_KEY,
    }
    status, data = msg91_request("GET", "/api/v5/otp/verify", params)
    verified = status == 200 and data.get("type") == "success"
    return jsonify(verified=verified, raw=data), (200 if verified else 400)


@app.route("/api/resend-otp", methods=["POST"])
def resend_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    retry_type = body.get("retrytype", "text")  # "text" or "voice"
    if not mobile:
        return jsonify(success=False, error="mobile is required"), 400
    if not MSG91_AUTH_KEY:
        return jsonify(success=False, error="Server OTP config missing"), 500

    params = {
        "mobile": normalize_mobile(mobile),
        "retrytype": retry_type,
        "authkey": MSG91_AUTH_KEY,
    }
    status, data = msg91_request("GET", "/api/v5/otp/retry", params)
    ok = status == 200 and data.get("type") == "success"
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", database=bool(Session))


def db_required(fn):
    def wrapper(*args, **kwargs):
        if not Session:
            return jsonify(error="Database not configured (DATABASE_URL missing)"), 500
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")
if not ADMIN_API_KEY:
    print(
        "WARNING: ADMIN_API_KEY is not set. Admin endpoints (booking lookup, "
        "coupon/campaign/markup management) will refuse all requests until "
        "it's configured. This is intentional — do not set a default value."
    )


def require_admin_key(fn):
    """Guards endpoints that expose PII or control pricing/promotions.
    NOTE: a single shared key is a stopgap for pre-prod, not real
    multi-user auth with roles — replace with per-user accounts + roles
    before this handles real agents/admins with different permissions."""
    def wrapper(*args, **kwargs):
        if not ADMIN_API_KEY:
            return jsonify(error="Admin API key not configured on server"), 500
        provided = request.headers.get("X-Admin-Key")
        if not provided or provided != ADMIN_API_KEY:
            return jsonify(error="Unauthorized"), 401
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------- Bookings ----------

@app.route("/api/bookings", methods=["POST"])
@db_required
def create_booking():
    """
    Financial-integrity note: `net` (the airline/hotel fare) is still
    accepted from the client, because this backend has no live GDS/hotel
    feed of its own yet to independently verify it against — that remains
    a real gap until a real fare provider is integrated server-side.

    What IS now enforced server-side, not trusted from the client:
    platformFee (recomputed from the stored MarkupConfig), discount
    (recomputed from the actual Coupon row, not whatever number the
    client sends), and total (always recalculated, never accepted as-is).
    A tampered or buggy frontend can no longer just declare its own total.
    """
    body = request.get_json(silent=True) or {}
    required = ["bookingId", "pnr", "kind", "title", "net", "role"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify(error=f"Missing fields: {', '.join(missing)}"), 400

    try:
        net = int(body["net"])
        agent_fee = max(0, int(body.get("agentFee", 0)))
        if net < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(error="net and agentFee must be non-negative numbers"), 400

    traveler = body.get("traveler", {})
    coupon_code = (body.get("couponCode") or "").strip().upper()

    session = Session()
    try:
        if session.get(Booking, body["bookingId"]):
            return jsonify(error="bookingId already exists"), 409

        # Recompute platform fee from the server's own markup config.
        cfg = session.get(MarkupConfig, 1)
        if not cfg:
            cfg = MarkupConfig(id=1, type="percent", value=8.0)
            session.add(cfg)
        platform_fee = (
            round(net * cfg.value / 100) if cfg.type == "percent" else round(cfg.value)
        )

        # Recompute discount from the real coupon row, if one was named —
        # never trust a client-supplied discount amount directly.
        discount = 0
        if coupon_code:
            coupon = session.get(Coupon, coupon_code)
            if not coupon:
                return jsonify(error="Coupon not found"), 400
            today = datetime.utcnow().strftime("%Y-%m-%d")
            if today < coupon.valid_from or today > coupon.valid_to:
                return jsonify(error="Coupon not valid on this date"), 400
            if coupon.used >= coupon.max_uses:
                return jsonify(error="Coupon usage limit reached"), 400
            subtotal_before_discount = net + platform_fee + agent_fee
            discount = (
                round(subtotal_before_discount * coupon.value / 100)
                if coupon.type == "percent" else round(coupon.value)
            )
            discount = min(discount, subtotal_before_discount)
            # Atomic, race-safe redemption: only succeeds if still under cap.
            result = session.query(Coupon).filter(
                Coupon.code == coupon_code, Coupon.used < Coupon.max_uses
            ).update({Coupon.used: Coupon.used + 1})
            if result == 0:
                session.rollback()
                return jsonify(error="Coupon usage limit reached"), 400

        total = net + platform_fee + agent_fee - discount

        booking = Booking(
            booking_id=body["bookingId"],
            partner_ref=body["pnr"],
            kind=body["kind"],
            title=body["title"],
            net_fare=net,
            platform_fee=platform_fee,
            agent_fee=agent_fee,
            discount=discount,
            total=total,
            booked_by_role=body["role"],
            traveler_name=traveler.get("name"),
            traveler_phone=traveler.get("phone"),
            traveler_phone_verified=traveler.get("phoneVerified", False),
            traveler_email=traveler.get("email"),
            details=body.get("details", {}),
        )
        session.add(booking)
        session.commit()
        return jsonify(success=True, booking=booking.to_dict()), 201
    finally:
        session.close()


@app.route("/api/bookings", methods=["GET"])
@require_admin_key
@db_required
def list_bookings():
    role = request.args.get("role")
    session = Session()
    try:
        q = session.query(Booking).order_by(Booking.created_at.desc())
        if role:
            q = q.filter(Booking.booked_by_role == role)
        return jsonify([b.to_dict() for b in q.all()])
    finally:
        session.close()


@app.route("/api/bookings/<id_or_ref>", methods=["GET"])
@require_admin_key
@db_required
def get_booking(id_or_ref):
    session = Session()
    try:
        booking = (
            session.get(Booking, id_or_ref)
            or session.query(Booking).filter(Booking.partner_ref == id_or_ref).first()
        )
        if not booking:
            return jsonify(error="Not found"), 404
        return jsonify(booking.to_dict())
    finally:
        session.close()


# ---------- Coupons ----------

@app.route("/api/coupons", methods=["POST"])
@require_admin_key
@db_required
def create_coupon():
    body = request.get_json(silent=True) or {}
    required = ["code", "type", "value", "from", "to"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify(error=f"Missing fields: {', '.join(missing)}"), 400

    session = Session()
    try:
        code = body["code"].strip().upper()
        if session.get(Coupon, code):
            return jsonify(error="Coupon code already exists"), 409
        coupon = Coupon(
            code=code, type=body["type"], value=body["value"],
            valid_from=body["from"], valid_to=body["to"],
            max_uses=body.get("max", 1), used=0,
        )
        session.add(coupon)
        session.commit()
        return jsonify(success=True, coupon=coupon.to_dict()), 201
    finally:
        session.close()


@app.route("/api/coupons", methods=["GET"])
@require_admin_key
@db_required
def list_coupons():
    session = Session()
    try:
        return jsonify([c.to_dict() for c in session.query(Coupon).all()])
    finally:
        session.close()


@app.route("/api/coupons/<code>", methods=["DELETE"])
@require_admin_key
@db_required
def delete_coupon(code):
    session = Session()
    try:
        coupon = session.get(Coupon, code.strip().upper())
        if not coupon:
            return jsonify(error="Not found"), 404
        session.delete(coupon)
        session.commit()
        return jsonify(success=True)
    finally:
        session.close()


@app.route("/api/coupons/<code>/use", methods=["POST"])
@db_required
def use_coupon(code):
    """Validates the coupon (date window + usage cap) and atomically
    increments its usage count via a single conditional UPDATE, so two
    concurrent requests can't both succeed past the usage cap. Call this
    at the moment a booking is confirmed, not just when the code is
    applied at search time. (Note: /api/bookings does this same check
    internally when a couponCode is passed to it directly.)"""
    code = code.strip().upper()
    session = Session()
    try:
        coupon = session.get(Coupon, code)
        if not coupon:
            return jsonify(valid=False, error="Coupon not found"), 404
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today < coupon.valid_from or today > coupon.valid_to:
            return jsonify(valid=False, error="Coupon not valid on this date"), 400

        result = session.query(Coupon).filter(
            Coupon.code == code, Coupon.used < Coupon.max_uses
        ).update({Coupon.used: Coupon.used + 1})
        if result == 0:
            session.rollback()
            return jsonify(valid=False, error="Coupon usage limit reached"), 400
        session.commit()
        session.refresh(coupon)
        return jsonify(valid=True, coupon=coupon.to_dict())
    finally:
        session.close()


# ---------- Campaigns ----------

@app.route("/api/campaigns", methods=["POST"])
@require_admin_key
@db_required
def create_campaign():
    body = request.get_json(silent=True) or {}
    if not body.get("title"):
        return jsonify(error="title is required"), 400
    session = Session()
    try:
        campaign = Campaign(title=body["title"], tag=body.get("tag", ""), active=True)
        session.add(campaign)
        session.commit()
        return jsonify(success=True, campaign=campaign.to_dict()), 201
    finally:
        session.close()


@app.route("/api/campaigns", methods=["GET"])
@db_required
def list_campaigns():
    session = Session()
    try:
        q = session.query(Campaign).order_by(Campaign.created_at.desc())
        return jsonify([c.to_dict() for c in q.all()])
    finally:
        session.close()


@app.route("/api/campaigns/<int:campaign_id>", methods=["PATCH"])
@require_admin_key
@db_required
def toggle_campaign(campaign_id):
    session = Session()
    try:
        campaign = session.get(Campaign, campaign_id)
        if not campaign:
            return jsonify(error="Not found"), 404
        campaign.active = not campaign.active
        session.commit()
        return jsonify(success=True, campaign=campaign.to_dict())
    finally:
        session.close()


# ---------- Markup config ----------

@app.route("/api/markup-config", methods=["GET"])
@db_required
def get_markup_config():
    session = Session()
    try:
        cfg = session.get(MarkupConfig, 1)
        if not cfg:
            cfg = MarkupConfig(id=1, type="percent", value=8.0)
            session.add(cfg)
            session.commit()
        return jsonify(cfg.to_dict())
    finally:
        session.close()


@app.route("/api/markup-config", methods=["PUT"])
@require_admin_key
@db_required
def set_markup_config():
    body = request.get_json(silent=True) or {}
    if "type" not in body or "value" not in body:
        return jsonify(error="type and value are required"), 400
    session = Session()
    try:
        cfg = session.get(MarkupConfig, 1)
        if not cfg:
            cfg = MarkupConfig(id=1)
            session.add(cfg)
        cfg.type = body["type"]
        cfg.value = body["value"]
        session.commit()
        return jsonify(success=True, config=cfg.to_dict())
    finally:
        session.close()


if __name__ == "__main__":
    # Debug mode only for local development — turn off in production.
    app.run(host="0.0.0.0", port=5000, debug=True)
