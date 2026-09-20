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
from urllib.parse import urlencode
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, g
from flask_cors import CORS

from models import make_session, Booking, Coupon, Campaign, MarkupConfig, Agent, AdminUser, AuditLog

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
RESEND_HOST = "api.resend.com"

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


def send_email_via_resend(to_email, subject, html_body, attachment_filename=None, attachment_base64=None):
    """Sends one email via Resend's REST API. Returns (ok: bool, raw response dict)."""
    if not RESEND_API_KEY:
        return False, {"error": "RESEND_API_KEY not configured on server"}

    payload = {
        "from": f"Tripbookerz <{EMAIL_FROM}>",
        "to": [to_email],
        "cc": ["support@tripbookerz.com"],
        "subject": subject,
        "html": html_body,
    }
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
    """Returns (ok: bool, raw response dict). Shared by /api/send-otp and agent registration."""
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
    """Returns (verified: bool, raw response dict). Shared by /api/verify-otp and agent registration."""
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


@app.route("/api/send-otp", methods=["POST"])
@rate_limit(max_calls=5, window_seconds=600)  # 5 per 10 min per IP
def send_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    if not mobile:
        return jsonify(success=False, error="mobile is required"), 400
    ok, data = msg91_send_otp(mobile)
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/api/verify-otp", methods=["POST"])
@rate_limit(max_calls=10, window_seconds=600)  # 10 attempts per 10 min per IP
def verify_otp():
    body = request.get_json(silent=True) or {}
    mobile = body.get("mobile", "")
    otp = body.get("otp", "")
    if not mobile or not otp:
        return jsonify(success=False, error="mobile and otp are required"), 400
    verified, data = msg91_verify_otp(mobile, otp)
    return jsonify(verified=verified, raw=data), (200 if verified else 400)


@app.route("/api/resend-otp", methods=["POST"])
@rate_limit(max_calls=3, window_seconds=600)  # 3 per 10 min per IP
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

    if not to_email or not subject or not html_body:
        return jsonify(success=False, error="to, subject, and html are required"), 400
    if not RESEND_API_KEY:
        return jsonify(success=False, error="Email sending is not configured on the server"), 500

    ok, data = send_email_via_resend(to_email, subject, html_body, pdf_filename, pdf_base64)
    return jsonify(success=ok, raw=data), (200 if ok else 502)


@app.route("/health", methods=["GET"])
def health():
    # Deliberately minimal — no auth required, so it must not leak
    # anything useful for reconnaissance (env, DB connectivity, internal
    # implementation details). See /api/admin/diagnostics for those.
    return jsonify(status="ok")


@app.route("/api/admin/diagnostics", methods=["GET"])
@require_admin_auth
def admin_diagnostics():
    return jsonify(
        database=bool(Session),
        deploy_env=DEPLOY_ENV,
        rate_limiter="in-memory (single-instance only)",
    )


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
        ok, _ = msg91_send_otp(body["phone"])
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
        verified, _ = msg91_verify_otp(agent.phone, otp)
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
