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
import secrets
import base64
from urllib.parse import urlencode
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, g
from flask_cors import CORS

from models import make_session, Booking, Coupon, Campaign, MarkupConfig, Agent, AdminUser, AuditLog, CallbackRequest, BookingAttempt, FlightProviderLog
import flight_providers

app = Flask(__name__)

# DEPLOY_ENV drives every environment-specific behavior in this file
# (CORS below, and the rate-limiter startup gate further down) — defined
# once, here, at the top, so there's a single source of truth instead of
# scattered manual-edit comments.
DEPLOY_ENV = os.environ.get("DEPLOY_ENV", "staging").lower()

# CORS: staging defaults to allow-all for easy testing from any device/
# tool without needing to know the exact origin in advance. Production
# requires an explicit comma-separated allowlist and refuses to start
# without one — wide-open CORS must never carry into production by default.
_cors_origins_raw = os.environ.get("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

if DEPLOY_ENV == "production":
    if not _cors_origins:
        raise SystemExit(
            "REFUSING TO START: DEPLOY_ENV=production requires CORS_ORIGINS "
            "to be set explicitly — a comma-separated list of allowed "
            "origins, e.g. 'https://www.tripbookerz.com'. Wide-open CORS "
            "('*') is the staging default and must not reach production "
            "silently."
        )
    CORS(app, origins=_cors_origins)
elif _cors_origins:
    # Staging with an explicit allowlist set — honor it (useful for
    # testing the production-like restricted behavior before flipping
    # DEPLOY_ENV itself).
    CORS(app, origins=_cors_origins)
else:
    CORS(app)  # staging default: allow all

MSG91_AUTH_KEY = os.environ.get("MSG91_AUTH_KEY")
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID")
MSG91_HOST = "control.msg91.com"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# Must be an address on a domain you've verified in Resend's dashboard
# (DNS records). Until that's done, Resend only allows sending from its
# own sandbox address — see .env.example / README for the exact caveat.
EMAIL_FROM = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")

# Every business email address the backend refers to is configurable —
# addresses like this can and do change over time (new support inbox,
# company rebrand, etc.), and hardcoding them means a code change +
# redeploy for what should be a one-line env var edit.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@tripbookerz.com")
# The domain allowed for client-supplied CC addresses (see
# send_confirmation_email below) is DERIVED from SUPPORT_EMAIL's own
# domain, not a separate env var — so there's one thing to set, not two
# that could drift out of sync. If a real case ever needs CC allowed on
# a different domain than support's own, split this into its own var
# then; not needed today.
_CC_ALLOWED_DOMAIN = "@" + SUPPORT_EMAIL.rsplit("@", 1)[-1].lower()
RESEND_HOST = "api.resend.com"

def redact_db_url(url):
    """For safe logging — never print a connection string with its
    password in it. postgresql://user:PASSWORD@host/db -> user:***@host/db"""
    try:
        if "://" not in url or "@" not in url:
            return "(unrecognized format)"
        scheme, rest = url.split("://", 1)
        creds, hostpart = rest.split("@", 1)
        user = creds.split(":", 1)[0] if ":" in creds else creds
        return f"{scheme}://{user}:***@{hostpart}"
    except Exception:
        return "(could not parse for logging)"


DATABASE_URL = os.environ.get("DATABASE_URL")
Session = None
if DATABASE_URL:
    try:
        Session = make_session(DATABASE_URL)
    except Exception as e:
        # A bad/unreachable DATABASE_URL must not crash the whole app —
        # that would take down OTP sending, email, everything, over a
        # problem that only actually affects DB-backed features. Log
        # enough to actually debug it (exception type + message, and a
        # password-redacted version of the URL) without crashing.
        print(
            f"ERROR: Could not connect to the database at startup — DB-backed "
            f"endpoints will return 500s until this is fixed, but the rest of "
            f"the app (OTP, email) will keep working.\n"
            f"  URL (redacted): {redact_db_url(DATABASE_URL)}\n"
            f"  Error: {type(e).__name__}: {e}"
        )
        Session = None
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

if not RESEND_API_KEY:
    print(
        "WARNING: RESEND_API_KEY is not set. Booking confirmation emails "
        "will fail until it's configured. See .env.example."
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


def resend_send_email(to_email, subject, html_body, attachment_filename=None, attachment_base64=None, cc=None, reply_to=None):
    """Sends one email via Resend's REST API. Returns (ok: bool, raw response dict).
    Same (ok, raw) shape as every OTP provider adapter — see the
    EMAIL_PROVIDER dispatch table below for why that consistency matters.

    cc and reply_to are deliberately caller-supplied, not hardcoded here —
    WHO gets CC'd is business logic (e.g. "only CC sales.holiday@... for
    holiday-package bookings, plus the assisting agent's own email if
    any"), decided by the caller with full context of the booking, not
    something a generic email-sending adapter should assume for every
    email it's ever asked to send."""
    if not RESEND_API_KEY:
        return False, {"error": "RESEND_API_KEY not configured on server"}

    payload = {
        "from": f"Tripbookerz <{EMAIL_FROM}>",
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    if cc:
        payload["cc"] = cc
    if reply_to:
        payload["reply_to"] = reply_to
    if attachment_filename and attachment_base64:
        payload["attachments"] = [{"filename": attachment_filename, "content": attachment_base64}]

    conn = http.client.HTTPSConnection(RESEND_HOST)
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    conn.request("POST", "/emails", body=json.dumps(payload), headers=headers)
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"message": raw}
    ok = res.status in (200, 201) and "id" in data
    return ok, data


# ---------- Generic email provider dispatch ----------
# Mirrors the OTP_PROVIDER pattern exactly (see send_otp_via_provider
# above) so both third-party integrations in this app follow the same
# shape — one thing to learn, reused twice, rather than two different
# patterns to remember.
#
# TO ADD A NEW EMAIL PROVIDER (e.g. SendGrid, Postmark) LATER:
#   1. Write one function: your_provider_send_email(to_email, subject,
#      html_body, attachment_filename=None, attachment_base64=None,
#      cc=None, reply_to=None) returning (ok: bool, raw_response: dict)
#      — same shape as resend_send_email above.
#   2. Add one line to _EMAIL_PROVIDERS below.
#   3. Set EMAIL_PROVIDER=your_provider_name as an env var.
# Nothing else in this file changes — every caller uses
# send_email_via_provider(), never resend_send_email() directly.
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "resend").lower()
_EMAIL_PROVIDERS = {
    "resend": resend_send_email,
}
if EMAIL_PROVIDER not in _EMAIL_PROVIDERS:
    print(f"WARNING: EMAIL_PROVIDER='{EMAIL_PROVIDER}' is not recognized (expected one of: "
          f"{', '.join(_EMAIL_PROVIDERS.keys())}). Falling back to 'resend'.")
    EMAIL_PROVIDER = "resend"


def send_email_via_provider(to_email, subject, html_body, attachment_filename=None, attachment_base64=None, cc=None, reply_to=None):
    return _EMAIL_PROVIDERS[EMAIL_PROVIDER](to_email, subject, html_body, attachment_filename, attachment_base64, cc, reply_to)


# ---------- Generic payment provider dispatch ----------
# Mirrors OTP_PROVIDER / EMAIL_PROVIDER exactly — same reasoning: one
# thing to learn, reused a third time, rather than a new pattern.
#
# HONEST STATE TODAY: the only implementation is mock_process_payment
# below. It does NOT charge anything real, and the reference number it
# returns is NOT issued by any real payment gateway — "PAYMENT_PROVIDER"
# defaults to "mock" and there is no other option implemented yet. This
# exists so the REST of the system (this endpoint, the database schema,
# the invoice, traceability) is already built against the real shape a
# genuine gateway integration will have. Swapping in a real one (Razorpay,
# Stripe, PayU) later means:
#   1. Sign up with that provider and get real API credentials — this
#      needs a real merchant account, which is outside what can be set
#      up from here.
#   2. Write one function: your_provider_process_payment(amount,
#      currency, method) returning (ok: bool, reference: str, raw: dict)
#      — same shape as mock_process_payment below.
#   3. Add one line to _PAYMENT_PROVIDERS below.
#   4. Set PAYMENT_PROVIDER=your_provider_name and that provider's
#      credentials as env vars.
# Nothing else in this file, or the frontend, needs to change — every
# caller uses process_payment_via_provider(), never a specific
# provider's function directly.
def mock_process_payment(amount, currency, method):
    """Returns (success: bool, reference: str, raw: dict). Simulates an
    immediate successful charge for a given amount/method. NOT a real
    payment gateway — see the module-level comment above."""
    reference = "MOCK_PAY_" + datetime.utcnow().strftime("%y%m%d") + "_" + secrets.token_hex(6).upper()
    return True, reference, {
        "amount": amount, "currency": currency, "method": method,
        "simulated": True, "note": "No real payment gateway is integrated yet.",
    }

PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "mock").lower()
_PAYMENT_PROVIDERS = {
    "mock": mock_process_payment,
}
# Which registered providers are mocks (silently "succeed" with fake
# data) vs real (actually call a real gateway). Kept right next to the
# registry itself so adding a real provider later means flipping its
# entry to False in the SAME edit that adds it — not a separate,
# easy-to-forget step. See enforce_no_mock_providers_in_production()
# below for what this is actually used for.
_PAYMENT_PROVIDERS_IS_MOCK = {
    "mock": True,
}
if PAYMENT_PROVIDER not in _PAYMENT_PROVIDERS:
    print(f"WARNING: PAYMENT_PROVIDER='{PAYMENT_PROVIDER}' is not recognized (expected one of: "
          f"{', '.join(_PAYMENT_PROVIDERS.keys())}). Falling back to 'mock'.")
    PAYMENT_PROVIDER = "mock"


def process_payment_via_provider(amount, currency, method):
    return _PAYMENT_PROVIDERS[PAYMENT_PROVIDER](amount, currency, method)


# ---------- Production safety net: refuse to silently serve fake data ----------
# Same principle as the CORS_ORIGINS check above, generalized: a
# misconfigured or forgotten env var should be a loud failure at
# startup, not a silent one discovered later from a customer complaint
# or a finance reconciliation mismatch. This specifically targets
# providers that SUCCEED with fake data rather than erroring — OTP and
# email providers already fail loudly if unconfigured (a missing API
# key means a real error, not a fake success), so they don't need this;
# payment and flight fares are the two that currently CAN silently
# fake success, so those are the two checked today.
#
# TO COVER A NEW MODULE LATER (e.g. a real hotel-provider aggregator
# gets built the same way flights was): add one entry to
# MOCK_PROVIDER_CHECKS below — (label, active provider name(s) as a
# list, that category's is-mock dict). Nothing else about this function
# needs to change.
MOCK_PROVIDER_CHECKS = [
    ("PAYMENT_PROVIDER", [PAYMENT_PROVIDER], _PAYMENT_PROVIDERS_IS_MOCK),
    ("FLIGHT_PROVIDERS_ENABLED", flight_providers.FLIGHT_PROVIDERS_ENABLED, flight_providers.FLIGHT_PROVIDERS_IS_MOCK),
]


def enforce_no_mock_providers_in_production():
    if DEPLOY_ENV != "production":
        return
    problems = []
    for env_var_name, active_providers, is_mock_map in MOCK_PROVIDER_CHECKS:
        mock_active = [p for p in active_providers if is_mock_map.get(p, False)]
        if mock_active:
            problems.append(f"{env_var_name} has mock provider(s) active: {', '.join(mock_active)}")
    if problems:
        raise SystemExit(
            "REFUSING TO START: DEPLOY_ENV=production but the following "
            "would silently serve simulated/fake data to real customers:\n  - "
            + "\n  - ".join(problems)
            + "\nThis is deliberate — a mock succeeding silently (rather than "
              "erroring) is exactly the kind of misconfiguration that's easy "
              "to miss until a customer or a finance reconciliation catches "
              "it. Set these to real, non-mock provider names before "
              "deploying to production, or unset DEPLOY_ENV=production if "
              "this is intentionally a staging-like environment."
        )


enforce_no_mock_providers_in_production()

def get_client_ip():
    """Returns the real client IP, trusting only the hop closest to this
    server (Render's own edge proxy), not whatever a client claims.

    X-Forwarded-For is a comma-separated chain that proxies APPEND to,
    they don't replace it. With exactly one trusted proxy (Render) in
    front of this app, the LAST entry is the one Render's edge itself
    observed and added — trustworthy. Any earlier entries could be
    supplied by the client itself and are not trustworthy. Taking the
    FIRST entry (the previous approach) let anyone bypass IP-based rate
    limiting by just sending a different fake X-Forwarded-For value on
    every request."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "unknown"


def hash_token(token: str) -> str:
    """Session tokens are stored hashed, never in plaintext — a database
    compromise shouldn't also mean every active session is immediately
    usable by whoever has the data."""
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_mobile(mobile: str) -> str:
    """Ensure the number carries the 91 country code, as MSG91 expects."""
    mobile = "".join(ch for ch in mobile if ch.isdigit())
    if len(mobile) == 10:
        return "91" + mobile
    return mobile


# ---------- Rate limiting ----------
# Deliberately dependency-free (in-memory dict) rather than Flask-Limiter,
# since installing that couldn't be verified in the environment this was
# built in (no network access). Good enough for a SINGLE Render instance
# in pre-prod. Known limitation: this resets on every restart/redeploy,
# and won't work correctly if you ever scale to multiple instances/workers
# (each process has its own memory, so limits aren't shared between them).
#
# GUARDRAIL: rather than trust a comment or README note to be noticed,
# this refuses to even start if DEPLOY_ENV=production is set without an
# explicit opt-in — so accidentally deploying this weak limiter to real
# production requires a conscious, named decision, not a silent default.
from collections import defaultdict
import time

ALLOW_INMEMORY_RATE_LIMIT_IN_PROD = os.environ.get("ALLOW_INMEMORY_RATE_LIMIT_IN_PROD", "").lower() == "true"

if DEPLOY_ENV == "production" and not ALLOW_INMEMORY_RATE_LIMIT_IN_PROD:
    raise SystemExit(
        "REFUSING TO START: DEPLOY_ENV=production is set, but this backend "
        "still uses an in-memory, single-instance rate limiter — it will "
        "silently stop working correctly the moment you run more than one "
        "instance (each process gets its own separate counters, so real "
        "limits multiply by instance count with no error or warning). "
        "\n\nBefore going to real production, replace the rate limiter with "
        "Flask-Limiter + Redis (or similar shared store). If you understand "
        "this risk and want to proceed anyway (e.g. you're certain you'll "
        "only ever run one instance), set "
        "ALLOW_INMEMORY_RATE_LIMIT_IN_PROD=true explicitly — this is "
        "deliberately not a default, so this can't happen by accident."
    )

_rate_buckets = defaultdict(list)

def rate_limit(max_calls: int, window_seconds: int):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            ip = get_client_ip()
            key = f"{fn.__name__}:{ip}"
            now = time.time()
            bucket = _rate_buckets[key]
            # drop timestamps outside the window
            while bucket and bucket[0] < now - window_seconds:
                bucket.pop(0)
            if len(bucket) >= max_calls:
                return jsonify(error="Too many requests. Please wait and try again."), 429
            bucket.append(now)
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return decorator


def msg91_send_otp(mobile):
    """Returns (ok: bool, raw response dict)."""
    if not MSG91_AUTH_KEY or not MSG91_TEMPLATE_ID:
        return False, {"error": "Server OTP config missing"}
    params = {
        "template_id": MSG91_TEMPLATE_ID,
        "mobile": normalize_mobile(mobile),
        "authkey": MSG91_AUTH_KEY,
    }
    status, data = msg91_request("POST", "/api/v5/otp", params)
    ok = status == 200 and data.get("type") == "success"
    return ok, data


def msg91_verify_otp(mobile, otp):
    """Returns (verified: bool, raw response dict)."""
    if not MSG91_AUTH_KEY:
        return False, {"error": "Server OTP config missing"}
    params = {
        "mobile": normalize_mobile(mobile),
        "otp": otp,
        "authkey": MSG91_AUTH_KEY,
    }
    status, data = msg91_request("GET", "/api/v5/otp/verify", params)
    verified = status == 200 and data.get("type") == "success"
    return verified, data


# ---------- Message Central (VerifyNow) adapter ----------
# Chosen as an alternative to MSG91 specifically because its pre-approved
# shared DLT route lets sending start immediately, without MSG91's
# multi-day DLT registration wait.
#
# Structural difference from MSG91 that this adapter exists to hide:
# MSG91's verify step just needs (mobile, otp) again — nothing to carry
# over from the send call. Message Central's verify step needs the
# `verificationId` that ITS send call returned; passing the mobile number
# alone isn't enough for their API. Rather than surface that difference
# to every caller (routes, agent registration), this adapter caches the
# verificationId in memory, keyed by normalized mobile, so the rest of
# the app can keep calling send_otp(mobile) / verify_otp(mobile, otp) —
# identical signature regardless of which provider is behind it.
#
# Known limitation, same class as the rate limiter: this cache is
# in-memory and per-process. Fine for a single Render instance; would
# need a shared store (Redis) before scaling to multiple instances,
# same as already documented for rate limiting.
MC_HOST = "cpaas.messagecentral.com"
MC_CUSTOMER_ID = os.environ.get("MC_CUSTOMER_ID")
MC_PASSWORD = os.environ.get("MC_PASSWORD")
MC_EMAIL = os.environ.get("MC_EMAIL")

_mc_token = None
_mc_token_fetched_at = 0
_mc_token_ttl_seconds = 20 * 3600  # refresh well before their ~24h expiry
_mc_verification_ids = {}  # normalized mobile -> verificationId, in-memory only


def _mc_request(method, path, params=None, headers=None):
    conn = http.client.HTTPSConnection(MC_HOST)
    query = ("?" + urlencode(params)) if params else ""
    conn.request(method, path + query, headers=headers or {"accept": "*/*"})
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    conn.close()
    try:
        return res.status, json.loads(raw)
    except ValueError:
        return res.status, {"message": raw}


def _mc_get_token():
    """Fetches and caches an auth token. Message Central's own docs recommend
    caching rather than fetching one per call — TTL is roughly 24h."""
    global _mc_token, _mc_token_fetched_at
    if _mc_token and (time.time() - _mc_token_fetched_at) < _mc_token_ttl_seconds:
        return _mc_token
    if not (MC_CUSTOMER_ID and MC_PASSWORD):
        return None
    key = base64.b64encode(MC_PASSWORD.encode()).decode()
    params = {"customerId": MC_CUSTOMER_ID, "key": key, "scope": "NEW", "country": "91"}
    if MC_EMAIL:
        params["email"] = MC_EMAIL
    status, data = _mc_request("GET", "/auth/v1/authentication/token", params)
    if status == 200 and data.get("token"):
        _mc_token = data["token"]
        _mc_token_fetched_at = time.time()
        return _mc_token
    return None


def message_central_send_otp(mobile):
    """Returns (ok: bool, raw response dict). Same external signature as
    msg91_send_otp — the verificationId this provider needs internally is
    cached here, not exposed to the caller."""
    if not (MC_CUSTOMER_ID and MC_PASSWORD):
        return False, {"error": "Server OTP config missing (MC_CUSTOMER_ID/MC_PASSWORD)"}
    token = _mc_get_token()
    if not token:
        return False, {"error": "Could not authenticate with Message Central"}

    digits = "".join(ch for ch in mobile if ch.isdigit())
    ten_digit = digits[-10:] if len(digits) >= 10 else digits
    params = {"countryCode": "91", "flowType": "SMS", "mobileNumber": ten_digit, "otpLength": "6"}
    status, data = _mc_request("POST", "/verification/v3/send", params, headers={"authToken": token})
    ok = status == 200 and data.get("responseCode") == 200
    if ok:
        verification_id = data.get("data", {}).get("verificationId")
        if verification_id:
            _mc_verification_ids[normalize_mobile(mobile)] = verification_id
    return ok, data


def message_central_verify_otp(mobile, otp):
    """Returns (verified: bool, raw response dict). Same external signature
    as msg91_verify_otp."""
    verification_id = _mc_verification_ids.get(normalize_mobile(mobile))
    if not verification_id:
        return False, {"error": "No pending OTP found for this number — please request a new OTP"}
    token = _mc_get_token()
    if not token:
        return False, {"error": "Could not authenticate with Message Central"}

    params = {"verificationId": verification_id, "code": otp}
    status, data = _mc_request("GET", "/verification/v3/validateOtp", params, headers={"authToken": token})
    verified = (
        status == 200
        and data.get("responseCode") == 200
        and data.get("data", {}).get("verificationStatus") == "VERIFICATION_COMPLETED"
    )
    if verified:
        _mc_verification_ids.pop(normalize_mobile(mobile), None)
    return verified, data


# ---------- Generic OTP provider dispatch ----------
# THIS is what makes switching providers "just a config change": every
# route and every other function in this app calls send_otp_via_provider()/
# verify_otp_via_provider() below — never msg91_send_otp() or
# message_central_send_otp() directly. Named with the _via_provider suffix
# specifically so they can't collide with the /api/send-otp and
# /api/verify-otp Flask route functions, which are separately named
# send_otp()/verify_otp() for their URL — two same-named functions at
# module level would silently shadow each other.
# Adding a third provider later means writing its adapter (matching the
# same (ok, raw) / (verified, raw) return shape) and adding one line to
# the dict below — nothing else in the app needs to change.
OTP_PROVIDER = os.environ.get("OTP_PROVIDER", "msg91").lower()
_OTP_PROVIDERS = {
    "msg91": (msg91_send_otp, msg91_verify_otp),
    "message_central": (message_central_send_otp, message_central_verify_otp),
}
if OTP_PROVIDER not in _OTP_PROVIDERS:
    print(f"WARNING: OTP_PROVIDER='{OTP_PROVIDER}' is not recognized (expected 'msg91' or "
          f"'message_central'). Falling back to 'msg91'.")
    OTP_PROVIDER = "msg91"


def send_otp_via_provider(mobile):
    return _OTP_PROVIDERS[OTP_PROVIDER][0](mobile)


def verify_otp_via_provider(mobile, otp):
    return _OTP_PROVIDERS[OTP_PROVIDER][1](mobile, otp)


def msg91_resend_otp(mobile, retry_type):
    if not MSG91_AUTH_KEY:
        return False, {"error": "Server OTP config missing"}
    params = {"mobile": normalize_mobile(mobile), "retrytype": retry_type, "authkey": MSG91_AUTH_KEY}
    status, data = msg91_request("GET", "/api/v5/otp/retry", params)
    ok = status == 200 and data.get("type") == "success"
    return ok, data


def message_central_resend_otp(mobile, retry_type):
    # Message Central has no distinct voice/text "retry" endpoint in their
    # documented API — a resend is just another send, which naturally
    # issues a fresh verificationId (overwriting the cached one, which is
    # exactly the correct behavior: the old one should stop being valid).
    # retry_type is accepted for interface compatibility but not used.
    return message_central_send_otp(mobile)


_OTP_RESEND_PROVIDERS = {
    "msg91": msg91_resend_otp,
    "message_central": message_central_resend_otp,
}


def resend_otp_via_provider(mobile, retry_type):
    return _OTP_RESEND_PROVIDERS[OTP_PROVIDER](mobile, retry_type)


@app.route("/api/payments/process", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)  # 10 per 10 min per IP
def process_payment():
    """Public and rate-limited for the same reason send-otp is: a real
    customer triggers this right at checkout, without necessarily being
    logged in. Payment processing is server-authoritative, never trusted
    from the client — this endpoint exists specifically so the reference
    number and success/failure are DECIDED HERE, not just relayed from
    whatever the browser claims happened. See the PAYMENT_PROVIDER
    dispatch above for the honest current state (mock only, no real
    gateway integrated yet)."""
    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    currency = body.get("currency", "INR")
    method = body.get("method")

    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify(success=False, error="A valid positive amount is required"), 400
    if method not in {"Credit/Debit Card", "UPI", "Net Banking"}:
        return jsonify(success=False, error="Invalid payment method"), 400

    ok, reference, raw = process_payment_via_provider(amount, currency, method)
    return jsonify(success=ok, referenceNo=reference, raw=raw), (200 if ok else 402)


@app.route("/api/send-otp", methods=["POST"])
@rate_limit(max_calls=5, window_seconds=600)  # 5 per 10 min per IP
def send_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    if not mobile:
        return jsonify(success=False, error="mobile is required"), 400
    ok, data = send_otp_via_provider(mobile)
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/api/verify-otp", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)  # 10 attempts per 10 min per IP
def verify_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    otp = body.get("otp", "")
    if not mobile or not otp:
        return jsonify(success=False, error="mobile and otp are required"), 400
    verified, data = verify_otp_via_provider(mobile, otp)
    return jsonify(verified=verified, raw=data), (200 if verified else 400)


@app.route("/api/resend-otp", methods=["POST"])
@rate_limit(max_calls=3, window_seconds=600)  # 3 per 10 min per IP
def resend_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    retry_type = body.get("retrytype", "text")  # "text" or "voice" — MSG91-specific, ignored by other providers
    if not mobile:
        return jsonify(success=False, error="mobile is required"), 400
    ok, data = resend_otp_via_provider(mobile, retry_type)
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/api/send-confirmation-email", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)  # 10 per 10 min per IP
def send_confirmation_email():
    """Sends a real booking confirmation email via Resend, with the
    e-ticket/voucher PDF attached. Public and rate-limited for the same
    reason send-otp is: real customers need to trigger this without
    logging in, right after their booking is confirmed."""
    body = request.get_json(silent=True) or {}
    to_email = body.get("to")
    subject = body.get("subject")
    html_body = body.get("html")
    pdf_filename = body.get("pdfFilename")
    pdf_base64 = body.get("pdfBase64")
    cc = body.get("cc") or []

    if not to_email or not subject or not html_body:
        return jsonify(success=False, error="to, subject, and html are required"), 400

    # cc is caller-supplied (the frontend knows at send-time whether this
    # was a holiday-package booking and, if agent-assisted, which agent —
    # see attemptSendConfirmationEmail() in index.html) — but this is a
    # PUBLIC, unauthenticated endpoint, so an unrestricted client-supplied
    # cc list would let anyone use our sending domain/reputation to spam
    # arbitrary third parties. Enforce a domain allowlist as the actual
    # security boundary: only addresses on SUPPORT_EMAIL's own domain
    # (_CC_ALLOWED_DOMAIN, derived above) are accepted.
    # NOTE, an honest limitation: this confirms the address is on our own
    # domain, not that it's a real, currently-approved employee/team
    # address — that would need a DB lookup against a real employee
    # directory, which doesn't exist yet. Flagged here rather than
    # silently assumed solved.
    if not isinstance(cc, list) or len(cc) > 3:
        return jsonify(success=False, error="cc must be a list of at most 3 addresses"), 400
    for addr in cc:
        if not isinstance(addr, str) or not addr.lower().endswith(_CC_ALLOWED_DOMAIN):
            return jsonify(success=False, error=f"cc address not allowed: {addr}"), 400

    # reply_to is NOT accepted from the request body at all, deliberately
    # — there's no legitimate case where a caller needs to redirect where
    # replies go, and allowing it would let a caller silently hijack a
    # real customer's replies to an address of their choosing. Fixed
    # server-side to the one value that's ever actually correct.
    reply_to = SUPPORT_EMAIL

    # No provider-specific config check here deliberately — each provider
    # adapter already returns a clear (False, {"error": "...not configured..."})
    # when ITS OWN required config is missing (see resend_send_email above).
    # A hardcoded "if not RESEND_API_KEY" check here would incorrectly
    # block a different, properly-configured EMAIL_PROVIDER — exactly the
    # kind of thing that defeats the point of the abstraction.

    ok, data = send_email_via_provider(to_email, subject, html_body, pdf_filename, pdf_base64, cc, reply_to)
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/health", methods=["GET"])
def health():
    # Deliberately minimal — no auth required, so it must not leak
    # anything useful for reconnaissance (env, DB connectivity, internal
    # implementation details). See /api/admin/diagnostics for those.
    return jsonify(status="ok")


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


def resolve_admin_actor():
    """Checks X-Admin-Key (master key) OR Admin-Token (per-employee session).
    Returns an actor dict on success, or None if neither is valid."""
    provided_key = request.headers.get("X-Admin-Key")
    if ADMIN_API_KEY and provided_key and provided_key == ADMIN_API_KEY:
        return {"type": "master_key", "id": None, "name": "Master key"}

    token = request.headers.get("Admin-Token")
    if token and Session:
        session = Session()
        try:
            admin = session.query(AdminUser).filter(AdminUser.session_token == hash_token(token)).first()
            if admin and admin.status == "active" and admin.session_token_expires and datetime.utcnow() <= admin.session_token_expires:
                return {"type": "admin_user", "id": admin.admin_id, "name": admin.name}
        finally:
            session.close()
    return None


def require_admin_auth(fn):
    """Guards endpoints that expose PII or control pricing/promotions.
    Accepts EITHER the master X-Admin-Key (intended only for bootstrapping
    the first employee account(s) — see /api/admin/users) or a real
    per-employee Admin-Token from /api/admin/login. Whichever is used,
    the resolved actor is stashed on flask.g.actor so the route can log
    who actually performed the action."""
    def wrapper(*args, **kwargs):
        actor = resolve_admin_actor()
        if not actor:
            return jsonify(error="Unauthorized"), 401
        g.actor = actor
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def require_master_key(fn):
    """Stricter than require_admin_auth: ONLY the master X-Admin-Key works,
    never a per-employee token. Used exclusively for creating/disabling
    admin employee accounts, so a regular employee login can never be used
    to mint more employee accounts (no privilege-escalation chain)."""
    def wrapper(*args, **kwargs):
        provided_key = request.headers.get("X-Admin-Key")
        if not ADMIN_API_KEY or not provided_key or provided_key != ADMIN_API_KEY:
            return jsonify(error="Unauthorized — master key required for account management"), 401
        g.actor = {"type": "master_key", "id": None, "name": "Master key"}
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper



def log_audit(session, action, target=None, details=None):
    """Writes one append-only audit row using the actor resolved by
    require_admin_auth. Call this AFTER a successful mutation, before
    session.commit() (or in the same commit) so the log and the change
    land together."""
    actor = getattr(g, "actor", None) or {"type": "unknown", "id": None, "name": "Unknown"}
    ip = get_client_ip()
    entry = AuditLog(
        actor_type=actor["type"], actor_id=actor["id"], actor_name=actor["name"],
        action=action, target=target, details=details, ip_address=ip,
    )
    session.add(entry)


@app.route("/api/admin/diagnostics", methods=["GET"])
@require_admin_auth
def admin_diagnostics():
    return jsonify(
        database=bool(Session),
        deploy_env=DEPLOY_ENV,
        rate_limiter="in-memory (single-instance only)",
    )


# ---------- Agents: registration, verification, approval, login ----------
# Real authentication — this replaces the frontend's old "click Agent to
# become an agent" button with an actual account system: register with
# KYC-style details, verify phone (real, via MSG91) and email (code-based;
# actual email SENDING isn't wired up yet — no provider connected — so in
# staging the code is returned in the response so the flow can still be
# tested end-to-end; this must be fixed with a real email provider before
# production), then an admin must approve the account before it can log in.

import re
import hashlib
from werkzeug.security import generate_password_hash, check_password_hash

GST_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}$")

def generate_agent_id():
    ymd = datetime.utcnow().strftime("%y%m%d")
    rand = secrets.token_hex(3).upper()
    return "TBZAGT" + ymd + rand

def hash_password(pw):
    return generate_password_hash(pw)

def verify_password(pw, pw_hash):
    return check_password_hash(pw_hash, pw)

def new_session_token():
    return secrets.token_hex(32)  # 64-char opaque bearer token


@app.route("/api/agents/register", methods=["POST"])
@rate_limit(max_calls=5, window_seconds=3600)  # 5 registration attempts/hour/IP
@db_required
def register_agent():
    body = request.get_json(silent=True) or {}
    required = ["entityType", "name", "registeredAddress", "phone", "email", "password"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify(error=f"Missing fields: {', '.join(missing)}"), 400

    entity_type = body["entityType"]
    if entity_type not in ("individual", "company"):
        return jsonify(error="entityType must be 'individual' or 'company'"), 400

    if len(body["password"]) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400

    gst_no = (body.get("gstNo") or "").strip().upper() or None
    if entity_type == "company" and not gst_no:
        return jsonify(error="gstNo is required for company registrations"), 400
    if gst_no and not GST_RE.match(gst_no):
        return jsonify(error="gstNo format looks invalid (expected a 15-character GSTIN). Note: this checks format only, not against the government GSTN database."), 400

    session = Session()
    try:
        if session.query(Agent).filter(Agent.email == body["email"]).first():
            return jsonify(error="An account with this email already exists"), 409

        agent = Agent(
            agent_id=generate_agent_id(),
            entity_type=entity_type,
            name=body["name"],
            registered_address=body["registeredAddress"],
            phone=body["phone"],
            email=body["email"],
            gst_no=gst_no,
            pan_no=(body.get("panNo") or "").strip().upper() or None,
            password_hash=hash_password(body["password"]),
            status="pending_verification",
        )

        # Trigger real phone OTP via MSG91.
        ok, _ = send_otp_via_provider(body["phone"])
        if not ok:
            return jsonify(error="Could not send phone verification OTP. Check MSG91 config."), 502

        # Email verification code (actual sending not wired up yet — see note above).
        code = str(secrets.randbelow(900000) + 100000)
        agent.email_verification_code = code
        agent.email_verification_expires = datetime.utcnow() + timedelta(minutes=30)

        session.add(agent)
        session.commit()

        resp = {"success": True, "agentId": agent.agent_id, "status": agent.status}
        if DEPLOY_ENV != "production":
            resp["dev_note"] = "Email sending isn't configured yet — this code is exposed here ONLY because DEPLOY_ENV != production."
            resp["dev_email_code"] = code
        return jsonify(resp), 201
    finally:
        session.close()


@app.route("/api/agents/verify-phone", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)
@db_required
def verify_agent_phone():
    body = request.get_json(silent=True) or {}
    agent_id = body.get("agentId")
    otp = body.get("otp")
    if not agent_id or not otp:
        return jsonify(error="agentId and otp are required"), 400

    session = Session()
    try:
        agent = session.get(Agent, agent_id)
        if not agent:
            return jsonify(error="Agent not found"), 404
        verified, _ = verify_otp_via_provider(agent.phone, otp)
        if not verified:
            return jsonify(verified=False, error="Incorrect or expired OTP"), 400
        agent.phone_verified = True
        if agent.phone_verified and agent.email_verified:
            agent.status = "pending_approval"
        session.commit()
        return jsonify(verified=True, status=agent.status)
    finally:
        session.close()


@app.route("/api/agents/verify-email", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)
@db_required
def verify_agent_email():
    body = request.get_json(silent=True) or {}
    agent_id = body.get("agentId")
    code = body.get("code")
    if not agent_id or not code:
        return jsonify(error="agentId and code are required"), 400

    session = Session()
    try:
        agent = session.get(Agent, agent_id)
        if not agent:
            return jsonify(error="Agent not found"), 404
        if not agent.email_verification_code or agent.email_verification_code != code:
            return jsonify(verified=False, error="Incorrect code"), 400
        if not agent.email_verification_expires or datetime.utcnow() > agent.email_verification_expires:
            return jsonify(verified=False, error="Code expired — request a new one"), 400
        agent.email_verified = True
        agent.email_verification_code = None
        if agent.phone_verified and agent.email_verified:
            agent.status = "pending_approval"
        session.commit()
        return jsonify(verified=True, status=agent.status)
    finally:
        session.close()


@app.route("/api/agents/resend-email-code", methods=["POST"])
@rate_limit(max_calls=3, window_seconds=600)
@db_required
def resend_agent_email_code():
    body = request.get_json(silent=True) or {}
    agent_id = body.get("agentId")
    if not agent_id:
        return jsonify(error="agentId is required"), 400
    session = Session()
    try:
        agent = session.get(Agent, agent_id)
        if not agent:
            return jsonify(error="Agent not found"), 404
        code = str(secrets.randbelow(900000) + 100000)
        agent.email_verification_code = code
        agent.email_verification_expires = datetime.utcnow() + timedelta(minutes=30)
        session.commit()
        resp = {"success": True}
        if DEPLOY_ENV != "production":
            resp["dev_email_code"] = code
        return jsonify(resp)
    finally:
        session.close()


@app.route("/api/agents/login", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)
@db_required
def agent_login():
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    password = body.get("password")
    if not email or not password:
        return jsonify(error="email and password are required"), 400

    session = Session()
    try:
        agent = session.query(Agent).filter(Agent.email == email).first()
        # Deliberately generic error (don't reveal whether the email exists).
        if not agent or not verify_password(password, agent.password_hash):
            return jsonify(error="Invalid email or password"), 401
        if agent.status != "approved":
            return jsonify(error=f"Account not yet active (status: {agent.status})"), 403

        token = new_session_token()
        agent.session_token = hash_token(token)
        agent.session_token_expires = datetime.utcnow() + timedelta(hours=12)
        session.commit()
        return jsonify(success=True, token=token, agent=agent.to_dict())
    finally:
        session.close()


def require_agent_auth(fn):
    """Validates the Agent-Token header against an approved agent's active
    session, and passes the agent's dict as the first argument to the
    wrapped function."""
    def wrapper(*args, **kwargs):
        if not Session:
            return jsonify(error="Database not configured"), 500
        token = request.headers.get("Agent-Token")
        if not token:
            return jsonify(error="Agent-Token header required"), 401
        session = Session()
        try:
            agent = session.query(Agent).filter(Agent.session_token == hash_token(token)).first()
            if not agent or agent.status != "approved":
                return jsonify(error="Invalid session"), 401
            if not agent.session_token_expires or datetime.utcnow() > agent.session_token_expires:
                return jsonify(error="Session expired, please log in again"), 401
            return fn(agent.to_dict(), *args, **kwargs)
        finally:
            session.close()
    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("/api/agents/me", methods=["GET"])
@require_agent_auth
def agent_me(agent):
    return jsonify(agent)


# ---------- Admin: agent approval ----------

@app.route("/api/agents", methods=["GET"])
@require_admin_auth
@db_required
def list_agents():
    status_filter = request.args.get("status")
    session = Session()
    try:
        q = session.query(Agent).order_by(Agent.created_at.desc())
        if status_filter:
            q = q.filter(Agent.status == status_filter)
        return jsonify([a.to_dict() for a in q.all()])
    finally:
        session.close()


@app.route("/api/agents/<agent_id>/approve", methods=["POST"])
@require_admin_auth
@db_required
def approve_agent(agent_id):
    session = Session()
    try:
        agent = session.get(Agent, agent_id)
        if not agent:
            return jsonify(error="Not found"), 404
        if agent.status != "pending_approval":
            return jsonify(error=f"Cannot approve from status '{agent.status}' — phone and email must be verified first"), 400
        agent.status = "approved"
        agent.approved_at = datetime.utcnow()
        log_audit(session, "agent.approve", target=agent_id, details={"agentName": agent.name, "agentEmail": agent.email})
        session.commit()
        return jsonify(success=True, agent=agent.to_dict())
    finally:
        session.close()


@app.route("/api/agents/<agent_id>/reject", methods=["POST"])
@require_admin_auth
@db_required
def reject_agent(agent_id):
    session = Session()
    try:
        agent = session.get(Agent, agent_id)
        if not agent:
            return jsonify(error="Not found"), 404
        agent.status = "rejected"
        log_audit(session, "agent.reject", target=agent_id, details={"agentName": agent.name, "agentEmail": agent.email})
        session.commit()
        return jsonify(success=True, agent=agent.to_dict())
    finally:
        session.close()


# ---------- Admin employee accounts, login, audit log ----------

def generate_admin_id():
    ymd = datetime.utcnow().strftime("%y%m%d")
    rand = secrets.token_hex(3).upper()
    return "TBZADM" + ymd + rand


@app.route("/api/admin/users", methods=["POST"])
@require_master_key
@db_required
def create_admin_user():
    """Master-key-only, deliberately not reachable with a regular employee
    token — see require_master_key's docstring for why."""
    body = request.get_json(silent=True) or {}
    required = ["name", "email", "password"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify(error=f"Missing fields: {', '.join(missing)}"), 400
    if len(body["password"]) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400

    session = Session()
    try:
        if session.query(AdminUser).filter(AdminUser.email == body["email"]).first():
            return jsonify(error="An admin account with this email already exists"), 409
        admin = AdminUser(
            admin_id=generate_admin_id(),
            name=body["name"], email=body["email"],
            password_hash=generate_password_hash(body["password"]),
            status="active",
        )
        session.add(admin)
        log_audit(session, "admin_user.create", target=admin.admin_id, details={"name": body["name"], "email": body["email"]})
        session.commit()
        return jsonify(success=True, admin=admin.to_dict()), 201
    finally:
        session.close()


@app.route("/api/admin/users", methods=["GET"])
@require_master_key
@db_required
def list_admin_users():
    session = Session()
    try:
        return jsonify([a.to_dict() for a in session.query(AdminUser).order_by(AdminUser.created_at.desc()).all()])
    finally:
        session.close()


@app.route("/api/admin/users/<admin_id>/disable", methods=["POST"])
@require_master_key
@db_required
def disable_admin_user(admin_id):
    session = Session()
    try:
        admin = session.get(AdminUser, admin_id)
        if not admin:
            return jsonify(error="Not found"), 404
        admin.status = "disabled"
        admin.session_token = None  # immediately invalidate any active session
        log_audit(session, "admin_user.disable", target=admin_id, details={"name": admin.name, "email": admin.email})
        session.commit()
        return jsonify(success=True, admin=admin.to_dict())
    finally:
        session.close()


@app.route("/api/admin/login", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)
@db_required
def admin_login():
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    password = body.get("password")
    if not email or not password:
        return jsonify(error="email and password are required"), 400

    session = Session()
    try:
        admin = session.query(AdminUser).filter(AdminUser.email == email).first()
        # Deliberately generic error (don't reveal whether the email exists).
        if not admin or not check_password_hash(admin.password_hash, password):
            return jsonify(error="Invalid email or password"), 401
        if admin.status != "active":
            return jsonify(error="This account has been disabled"), 403

        token = secrets.token_hex(32)
        admin.session_token = hash_token(token)
        admin.session_token_expires = datetime.utcnow() + timedelta(hours=12)
        g.actor = {"type": "admin_user", "id": admin.admin_id, "name": admin.name}
        log_audit(session, "admin_user.login", target=admin.admin_id)
        session.commit()
        return jsonify(success=True, token=token, admin=admin.to_dict())
    finally:
        session.close()


@app.route("/api/admin/me", methods=["GET"])
@require_admin_auth
def admin_me():
    return jsonify(g.actor)


@app.route("/api/admin/audit-log", methods=["GET"])
@require_admin_auth
@db_required
def get_audit_log():
    """Any authenticated admin (master key or employee) can view the log —
    read access to 'who changed what' shouldn't itself require the master
    key. Filter with ?action=coupon.create or ?actorId=TBZADM..., and page
    with ?limit=50 (default 100, capped at 500)."""
    action_filter = request.args.get("action")
    actor_filter = request.args.get("actorId")
    limit = min(int(request.args.get("limit", 100)), 500)

    session = Session()
    try:
        q = session.query(AuditLog).order_by(AuditLog.created_at.desc())
        if action_filter:
            q = q.filter(AuditLog.action == action_filter)
        if actor_filter:
            q = q.filter(AuditLog.actor_id == actor_filter)
        return jsonify([e.to_dict() for e in q.limit(limit).all()])
    finally:
        session.close()


# ---------- Flight search (multi-provider aggregation) ----------
# Actual provider logic lives entirely in flight_providers.py — this
# route is deliberately thin: parse the request, call the aggregator,
# persist the provider-performance log, return the results. Nothing
# provider-specific belongs here; that's the whole point of the module
# split.

@app.route("/api/flights/search", methods=["POST"])
@rate_limit(max_calls=20, window_seconds=600)  # 20 per 10 min per IP — searching is cheap and frequent, higher limit than booking-mutation endpoints
def search_flights():
    body = request.get_json(silent=True) or {}
    from_code = (body.get("from") or "").strip().upper()
    to_code = (body.get("to") or "").strip().upper()
    date = (body.get("date") or "").strip()
    if not from_code or not to_code or not date:
        return jsonify(success=False, error="from, to, and date are all required"), 400
    if from_code == to_code:
        return jsonify(success=False, error="from and to cannot be the same"), 400

    results, provider_log = flight_providers.search_flights_aggregated(from_code, to_code, date)

    search_id = "SRCH" + secrets.token_hex(8).upper()
    if Session:
        session = Session()
        try:
            for entry in provider_log:
                session.add(FlightProviderLog(
                    search_id=search_id, from_code=from_code, to_code=to_code, search_date=date,
                    provider=entry["provider"], responded=entry["responded"], error=entry["error"],
                    offer_count=entry["offerCount"], lowest_fare=entry["lowestFare"], won_any_offer=entry["wonAnyOffer"],
                ))
            session.commit()
        except Exception as e:
            session.rollback()
            # A logging failure must never block real search results
            # from reaching the customer — log it server-side and move on.
            print(f"WARNING: failed to persist FlightProviderLog for {search_id}: {e}")
        finally:
            session.close()
    else:
        print(f"WARNING: DATABASE_URL not configured — FlightProviderLog for {search_id} was not persisted, but search results are still returned.")

    return jsonify(success=True, searchId=search_id, results=results)


@app.route("/api/flights/reprice", methods=["POST"])
@rate_limit(max_calls=20, window_seconds=600)
def reprice_flight():
    """THE GUARDRAIL, called right before payment. See reprice_offer()'s
    own docstring in flight_providers.py for the full contract — this
    route is just a thin wrapper that turns its result into HTTP."""
    body = request.get_json(silent=True) or {}
    token = body.get("offerToken")
    if not token:
        return jsonify(success=False, error="offerToken is required"), 400

    result = flight_providers.reprice_offer(token)
    if not result["ok"]:
        return jsonify(success=False, error=result["error"]), 409
    return jsonify(success=True, **{k: v for k, v in result.items() if k != "ok"})


# ---------- Bookings ----------

@app.route("/api/bookings", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)  # 10 per 10 min per IP
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
            # NOT trusted from the client — see the flag's definition in
            # models.py. A client claiming "true" here proves nothing;
            # the real fix (a short-lived server-issued token from
            # /api/verify-otp, required at booking time) is a larger
            # change tracked separately. Storing the client's claim as
            # fact would silently mislabel unverified data as verified.
            traveler_phone_verified=False,
            traveler_email=traveler.get("email"),
            # Accepted from the client for now, same documented trust
            # boundary as `net` above: the reference itself IS genuinely
            # server-generated (by /api/payments/process, called earlier
            # in the checkout flow — see PAYMENT_PROVIDER), but nothing
            # here re-verifies that THIS specific request actually came
            # from a real prior call to that endpoint before storing it.
            # Closing that gap needs a payments ledger (issued references
            # tracked and checked here), which is a larger addition than
            # this pass — same category of "known, not silently accepted"
            # gap as the unverified `net` fare.
            payment_mode=body.get("paymentMode"),
            payment_reference=body.get("paymentReference"),
            details=body.get("details", {}),
        )
        session.add(booking)
        session.commit()
        return jsonify(success=True, booking=booking.to_dict()), 201
    finally:
        session.close()


@app.route("/api/bookings", methods=["GET"])
@require_admin_auth
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
@require_admin_auth
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


@app.route("/api/bookings/lookup", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)
@db_required
def customer_lookup_booking():
    """Public self-service lookup for the support chat widget (and any
    other customer-facing 'track my booking' UI). Deliberately requires
    BOTH the booking ID and the phone number on that booking to match —
    a booking ID alone (which could be seen on a screen, shared, or
    guessed since it's not secret) must not be enough to pull up
    someone's travel/contact details. This is why this is a separate,
    stricter endpoint rather than just opening up get_booking() above,
    which has no such second factor and stays admin-only for that
    reason."""
    body = request.get_json(silent=True) or {}
    booking_id = (body.get("bookingId") or "").strip().upper()
    phone_digits = "".join(ch for ch in (body.get("phone") or "") if ch.isdigit())

    if not booking_id or not phone_digits:
        return jsonify(found=False, error="bookingId and phone are required"), 400

    session = Session()
    try:
        booking = (
            session.get(Booking, booking_id)
            or session.query(Booking).filter(Booking.partner_ref == booking_id).first()
        )
        if not booking:
            return jsonify(found=False, error="No booking found with that ID"), 404

        stored_phone = "".join(ch for ch in (booking.traveler_phone or "") if ch.isdigit())
        # Compare last 10 digits so a stored "91XXXXXXXXXX" still matches
        # a customer typing just the 10-digit number, without requiring
        # them to know exactly how the country code was stored.
        if not stored_phone or stored_phone[-10:] != phone_digits[-10:]:
            return jsonify(found=False, error="That booking ID and phone number don't match our records"), 404

        return jsonify(found=True, booking=booking.to_dict())
    finally:
        session.close()


@app.route("/api/bookings/<booking_id>/refund-status", methods=["PATCH"])
@require_admin_auth
@db_required
def update_refund_status(booking_id):
    """Admin-only — this is how a refund status becomes real instead of
    guessed. Nothing client-facing ever sets this; it only reflects what
    staff have actually recorded after processing (or rejecting) a
    cancellation."""
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    valid_statuses = {"none", "requested", "processing", "refunded", "rejected"}
    if new_status not in valid_statuses:
        return jsonify(error=f"status must be one of {sorted(valid_statuses)}"), 400

    session = Session()
    try:
        booking = session.get(Booking, booking_id)
        if not booking:
            return jsonify(error="Not found"), 404
        before = booking.refund_status
        booking.refund_status = new_status
        booking.refund_updated_at = datetime.utcnow()
        if new_status == "requested" and not booking.refund_requested_at:
            booking.refund_requested_at = datetime.utcnow()
        if "amount" in body:
            booking.refund_amount = body["amount"]
        if "notes" in body:
            booking.refund_notes = body["notes"]
        log_audit(session, "booking.refund_status_update", target=booking_id,
                  details={"before": before, "after": new_status})
        session.commit()
        return jsonify(success=True, booking=booking.to_dict())
    finally:
        session.close()


# ---------- Callback requests (holiday package lead capture) ----------

@app.route("/api/callback-requests", methods=["POST"])
@rate_limit(max_calls=5, window_seconds=600)
@db_required
def create_callback_request():
    """Public — the support chat calls this when someone asks about a
    holiday package. Deliberately captures a lead for a real staff
    callback rather than letting the bot try to sell/answer on its own,
    since packages are called out as the high-revenue product here."""
    body = request.get_json(silent=True) or {}
    phone_digits = "".join(ch for ch in (body.get("phone") or "") if ch.isdigit())
    if len(phone_digits) < 10:
        return jsonify(success=False, error="A valid phone number is required"), 400

    session = Session()
    try:
        req = CallbackRequest(
            phone=phone_digits,
            context=(body.get("context") or "")[:500],
            source=body.get("source", "chat_holiday_package"),
        )
        session.add(req)
        session.commit()
        return jsonify(success=True, id=req.id), 201
    finally:
        session.close()


@app.route("/api/callback-requests", methods=["GET"])
@require_admin_auth
@db_required
def list_callback_requests():
    status_filter = request.args.get("status")
    session = Session()
    try:
        q = session.query(CallbackRequest).order_by(CallbackRequest.created_at.desc())
        if status_filter:
            q = q.filter(CallbackRequest.status == status_filter)
        return jsonify([c.to_dict() for c in q.all()])
    finally:
        session.close()


@app.route("/api/callback-requests/<int:req_id>/status", methods=["PATCH"])
@require_admin_auth
@db_required
def update_callback_status(req_id):
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in {"new", "contacted", "closed"}:
        return jsonify(error="status must be new, contacted, or closed"), 400
    session = Session()
    try:
        req = session.get(CallbackRequest, req_id)
        if not req:
            return jsonify(error="Not found"), 404
        req.status = new_status
        if new_status == "contacted" and not req.contacted_at:
            req.contacted_at = datetime.utcnow()
        log_audit(session, "callback_request.status_update", target=str(req_id), details={"newStatus": new_status})
        session.commit()
        return jsonify(success=True, request=req.to_dict())
    finally:
        session.close()


@app.route("/api/booking-attempts", methods=["POST"])
@rate_limit(max_calls=15, window_seconds=600)  # a bit higher than bookings itself, since retries within one journey are expected
@db_required
def log_booking_attempt():
    """Public — called once per booking journey, whatever the outcome.
    This is what makes 'every failed booking is logged, and a booking
    that failed then succeeded on retry is logged with its attempt
    count' actually true, rather than just something the fault-tolerant
    retry logic silently handles client-side. Every field here is a
    snapshot the FRONTEND already knows by the time it calls this — this
    endpoint just records it, it doesn't compute anything new."""
    body = request.get_json(silent=True) or {}
    required = ["attemptId", "kind", "role", "finalStatus"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify(success=False, error=f"Missing fields: {', '.join(missing)}"), 400
    if body["finalStatus"] not in {"succeeded", "failed_partner", "failed_persistence"}:
        return jsonify(success=False, error="Invalid finalStatus"), 400

    traveler = body.get("traveler", {})
    session = Session()
    try:
        if session.get(BookingAttempt, body["attemptId"]):
            return jsonify(success=False, error="attemptId already logged"), 409
        attempt = BookingAttempt(
            attempt_id=body["attemptId"],
            kind=body["kind"],
            title=body.get("title"),
            net_fare=body.get("netFare"),
            role=body["role"],
            traveler_name=traveler.get("name"),
            traveler_phone=traveler.get("phone"),
            traveler_email=traveler.get("email"),
            details=body.get("details", {}),
            partner_outcome=body.get("partnerOutcome"),
            persistence_attempts=body.get("persistenceAttempts", 0),
            persistence_outcome=body.get("persistenceOutcome"),
            final_status=body["finalStatus"],
            final_booking_id=body.get("finalBookingId"),
            error_detail=(body.get("errorDetail") or "")[:500] or None,
        )
        session.add(attempt)
        session.commit()
        return jsonify(success=True), 201
    finally:
        session.close()


@app.route("/api/booking-attempts", methods=["GET"])
@require_admin_auth
@db_required
def list_booking_attempts():
    """?status=failed_partner|failed_persistence|succeeded to filter.
    ?hadRetries=true additionally filters to persistenceAttempts > 1 —
    the 'failed at first but eventually succeeded' view specifically
    called out as needing its own visibility, even though those rows
    also have finalStatus=succeeded and a real linked booking."""
    status_filter = request.args.get("status")
    had_retries = request.args.get("hadRetries") == "true"
    session = Session()
    try:
        q = session.query(BookingAttempt).order_by(BookingAttempt.created_at.desc())
        if status_filter:
            q = q.filter(BookingAttempt.final_status == status_filter)
        if had_retries:
            q = q.filter(BookingAttempt.persistence_attempts > 1)
        return jsonify([a.to_dict() for a in q.limit(500).all()])
    finally:
        session.close()


@app.route("/api/admin/flight-provider-logs", methods=["GET"])
@require_admin_auth
@db_required
def list_flight_provider_logs():
    """?provider=tbo to filter to one provider's history (the actual
    'evaluate this provider' view) — aggregate stats (response rate,
    win rate) are left to whoever's doing the evaluating to compute
    from these rows rather than baked into this endpoint, since the
    useful time window/grouping for that isn't fixed yet."""
    provider_filter = request.args.get("provider")
    session = Session()
    try:
        q = session.query(FlightProviderLog).order_by(FlightProviderLog.created_at.desc())
        if provider_filter:
            q = q.filter(FlightProviderLog.provider == provider_filter)
        return jsonify([e.to_dict() for e in q.limit(1000).all()])
    finally:
        session.close()


# ---------- Coupons ----------

@app.route("/api/coupons", methods=["POST"])
@require_admin_auth
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
        log_audit(session, "coupon.create", target=code, details={"type": body["type"], "value": body["value"], "from": body["from"], "to": body["to"], "max": body.get("max", 1)})
        session.commit()
        return jsonify(success=True, coupon=coupon.to_dict()), 201
    finally:
        session.close()


@app.route("/api/coupons", methods=["GET"])
@require_admin_auth
@db_required
def list_coupons():
    session = Session()
    try:
        return jsonify([c.to_dict() for c in session.query(Coupon).all()])
    finally:
        session.close()


@app.route("/api/coupons/<code>", methods=["DELETE"])
@require_admin_auth
@db_required
def delete_coupon(code):
    session = Session()
    try:
        coupon = session.get(Coupon, code.strip().upper())
        if not coupon:
            return jsonify(error="Not found"), 404
        log_audit(session, "coupon.delete", target=coupon.code, details={"type": coupon.type, "value": coupon.value, "used": coupon.used})
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
@require_admin_auth
@db_required
def create_campaign():
    body = request.get_json(silent=True) or {}
    if not body.get("title"):
        return jsonify(error="title is required"), 400
    session = Session()
    try:
        campaign = Campaign(title=body["title"], tag=body.get("tag", ""), active=True)
        session.add(campaign)
        session.flush()  # get campaign.id before logging, since it's autoincrement
        log_audit(session, "campaign.create", target=str(campaign.id), details={"title": body["title"], "tag": body.get("tag", "")})
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
@require_admin_auth
@db_required
def toggle_campaign(campaign_id):
    session = Session()
    try:
        campaign = session.get(Campaign, campaign_id)
        if not campaign:
            return jsonify(error="Not found"), 404
        campaign.active = not campaign.active
        log_audit(session, "campaign.toggle", target=str(campaign_id), details={"title": campaign.title, "newActive": campaign.active})
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
@require_admin_auth
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
        before = {"type": cfg.type, "value": cfg.value}
        cfg.type = body["type"]
        cfg.value = body["value"]
        log_audit(session, "markup_config.update", target="markup_config", details={"before": before, "after": {"type": body["type"], "value": body["value"]}})
        session.commit()
        return jsonify(success=True, config=cfg.to_dict())
    finally:
        session.close()


from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def handle_uncaught_exception(e):
    """Last-resort handler. Without this, an unexpected exception (a bug,
    a bad input that slipped past validation, a DB hiccup) would let
    Flask's default error page — which can include a stack trace — reach
    the client. Every response from here is a stable, generic shape;
    the real detail goes to server logs only."""
    if isinstance(e, HTTPException):
        return jsonify(error=e.description or "Request error"), e.code
    print(f"UNHANDLED ERROR: {type(e).__name__}: {e}")
    return jsonify(error="Internal server error"), 500


if __name__ == "__main__":
    # Debug mode only for local development — turn off in production.
    app.run(host="0.0.0.0", port=5000, debug=True)
