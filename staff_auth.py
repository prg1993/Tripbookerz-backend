"""
Staff portal authentication and account management (Stage 1).

Role/permission table: staff_permissions.py.

What this module does
  * two-step sign-in: password, then authenticator-app code (TOTP) -- for EVERY role
  * database-backed lockout (survives restarts / multiple workers)
  * server-side sessions (idle + absolute timeout, revocable)
  * staff account management (create, roles, disable, 2FA reset, password reset, unlock)
  * password reset by email link PLUS a second proof (authenticator or recovery code)
  * typed-reason requirement for Admin sign-in, user-management actions and
    sensitive customer-data reads, all written to the audit log
  * audit retention (retain_until) and a master-key-only purge

It plugs into app.py through init_staff_auth(app, deps); it does not import
app.py (no circular import).

Honest limits (also in the design doc): the reason is free text and cannot be
verified by the system; first-time 2FA enrolment is protected only by the
password the Admin handed over; the in-memory per-IP rate limiter is a second
layer, the database lockout is the real control.
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import request, jsonify, g
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

import totp
from models import AdminUser, AuditLog, StaffSession, PasswordResetToken
import staff_permissions as perms

_ctx = {}          # filled by init_staff_auth: Session factory, rate_limit, email sender, master-key check
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))   # equalises timing for unknown emails


# ---------------------------------------------------------------- config
def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def cfg():
    """Read at call time so Render env changes apply after a restart without code edits."""
    return {
        "max_attempts": _int("LOGIN_MAX_ATTEMPTS", 5),
        "lock_minutes": _int("LOGIN_LOCK_MINUTES", 15),
        "idle_minutes": _int("SESSION_IDLE_MINUTES", 30),
        "absolute_hours": _int("SESSION_ABSOLUTE_HOURS", 12),
        "challenge_minutes": _int("LOGIN_CHALLENGE_MINUTES", 5),
        "reset_minutes": _int("PASSWORD_RESET_MINUTES", 30),
        "password_min": _int("STAFF_PASSWORD_MIN_LENGTH", 12),
        "reason_min": _int("SENSITIVE_REASON_MIN_CHARS", 10),
        "audit_retain_days": _int("AUDIT_RETAIN_DEFAULT_DAYS", 365),
        "reason_enforced": _bool("SENSITIVE_REASON_ENFORCED", True),
        "portal_url": os.environ.get("STAFF_PORTAL_URL", "https://staff.tripbookerz.com").rstrip("/"),
    }


def legacy_login_enabled():
    """The old password-only /api/admin/login bypasses 2FA, so it is OFF unless
    explicitly switched on (LEGACY_ADMIN_LOGIN_ENABLED=true)."""
    return _bool("LEGACY_ADMIN_LOGIN_ENABLED", False)


# ---------------------------------------------------------------- small helpers
def now():
    return datetime.utcnow()


def th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_eq(a, b) -> bool:
    """Constant-time string comparison (never raises on non-ASCII)."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _client_ip():
    return _ctx["get_client_ip"]()


def _S():
    return _ctx.get("Session")


def _fernet():
    key = os.environ.get("STAFF_TOTP_ENC_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def _encrypt(plain: str):
    f = _fernet()
    return f.encrypt(plain.encode()).decode() if f else None


def _decrypt(token: str):
    f = _fernet()
    if not f or not token:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        return None


def roles_of(admin):
    return perms.normalize_roles(admin.roles)


def _gen_admin_id():
    return "TBZADM" + now().strftime("%y%m%d") + secrets.token_hex(3).upper()


def _password_problem(pw, email):
    c = cfg()
    if not isinstance(pw, str) or len(pw) < c["password_min"]:
        return f"Password must be at least {c['password_min']} characters"
    if len(pw) > 128:
        return "Password is too long (max 128 characters)"
    low = pw.lower()
    local = (email or "").split("@")[0].lower()
    if local and len(local) >= 3 and local in low:
        return "Password must not contain your email name"
    if "password" in low or "tripbookerz" in low:
        return "Password is too easy to guess"
    if len(set(pw)) < 5:
        return "Password is too repetitive"
    return None


def _body():
    """JSON body as a dict; anything else (list, string, missing) becomes {}."""
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def _reason_from(body):
    r = (body.get("reason") or "").strip() if isinstance(body, dict) else ""
    return r[:500]


def _need_reason(body):
    """Returns (reason, error_response|None)."""
    r = _reason_from(body)
    if len(r) < cfg()["reason_min"]:
        return None, (jsonify(error=f"A reason of at least {cfg()['reason_min']} characters is required", reasonRequired=True), 400)
    return r, None


# ---------------------------------------------------------------- audit
def write_audit(s, actor, action, target=None, details=None, reason=None, retain_until=None):
    """Append one audit row. actor = {type,id,name}. Caller commits."""
    if retain_until is None:
        retain_until = now() + timedelta(days=cfg()["audit_retain_days"])
    s.add(AuditLog(
        actor_type=actor.get("type", "unknown"), actor_id=actor.get("id"),
        actor_name=(actor.get("name") or "Unknown")[:255],
        action=action, target=(target[:255] if isinstance(target, str) else target),
        details=details, ip_address=_client_ip(),
        reason=reason, retain_until=retain_until,
    ))


def _actor_of(admin):
    return {"type": "staff_user", "id": admin.admin_id, "name": admin.name}


def _audit_now(actor, action, target=None, details=None, reason=None):
    """Audit in its own short transaction (used where the main one may roll back / not exist)."""
    S = _S()
    if not S:
        return
    s = S()
    try:
        write_audit(s, actor, action, target, details, reason)
        s.commit()
    except Exception as e:
        print(f"AUDIT WRITE FAILED ({action}): {type(e).__name__}")
    finally:
        s.close()


# ---------------------------------------------------------------- lockout
def _is_locked(admin):
    return bool(admin.locked_until and admin.locked_until > now())


def _register_failure(admin):
    c = cfg()
    admin.failed_attempts = (admin.failed_attempts or 0) + 1
    if admin.failed_attempts >= c["max_attempts"]:
        admin.locked_until = now() + timedelta(minutes=c["lock_minutes"])
        admin.failed_attempts = 0
        return True
    return False


# ---------------------------------------------------------------- 2FA primitives
def _totp_verify_and_consume(admin, code):
    secret = _decrypt(admin.totp_secret_enc)
    if not secret:
        return False
    step = totp.match_step(secret, code)
    if step is None or step <= (admin.totp_last_step or 0):
        return False
    admin.totp_last_step = step
    return True


def _recovery_use(admin, code):
    if not isinstance(code, str):
        return False
    norm = re.sub(r"[^0-9a-f]", "", code.lower())
    if len(norm) != 20:
        return False
    h = th(norm)
    codes = list(admin.recovery_codes or [])
    hit = None
    for c in codes:
        if hmac.compare_digest(c, h):
            hit = c
    if hit is None:
        return False
    codes.remove(hit)
    admin.recovery_codes = codes            # reassign so the JSON change is persisted
    return True


def _second_factor_ok(admin, code):
    code = (code or "").strip()
    return _totp_verify_and_consume(admin, code) or _recovery_use(admin, code)


def _make_recovery_codes(n=10):
    plain, hashed = [], []
    for _ in range(n):
        raw = secrets.token_hex(10)                     # 80 bits
        plain.append("-".join(raw[i:i + 5] for i in range(0, 20, 5)))
        hashed.append(th(raw))
    return plain, hashed


# ---------------------------------------------------------------- sessions
def _new_session_row(s, admin, kind, minutes=None, hours=None, reason=None):
    token = secrets.token_urlsafe(32)
    n = now()
    exp = n + (timedelta(minutes=minutes) if minutes is not None else timedelta(hours=hours))
    s.add(StaffSession(
        session_id=th(token), admin_id=admin.admin_id, kind=kind,
        created_at=n, last_seen_at=n, expires_at=exp,
        ip_address=_client_ip(), user_agent=(request.headers.get("User-Agent") or "")[:255],
        reason=reason,
    ))
    return token


def _revoke_all_sessions(s, admin_id, except_id=None):
    q = s.query(StaffSession).filter(StaffSession.admin_id == admin_id, StaffSession.revoked_at.is_(None))
    for row in q.all():
        if row.session_id != except_id:
            row.revoked_at = now()


def _get_challenge(s, token, kind):
    if not token or not isinstance(token, str) or len(token) > 200:
        return None, None
    row = s.get(StaffSession, th(token))
    if not row or row.kind != kind or row.revoked_at or row.expires_at < now():
        return None, None
    admin = s.get(AdminUser, row.admin_id)
    if not admin or admin.status != "active" or _is_locked(admin):
        return None, None
    return row, admin


def resolve_staff_actor(token):
    """Validate a Staff-Token. Returns actor dict or None. Enforces idle +
    absolute timeout, revocation, and that the account is still active."""
    S = _S()
    if not S or not token or not isinstance(token, str) or len(token) > 200:
        return None
    s = S()
    try:
        row = s.get(StaffSession, th(token))
        if not row or row.kind != "session" or row.revoked_at:
            return None
        n = now()
        c = cfg()
        if n > row.expires_at or (n - (row.last_seen_at or row.created_at)) > timedelta(minutes=c["idle_minutes"]):
            row.revoked_at = n
            s.commit()
            return None
        admin = s.get(AdminUser, row.admin_id)
        if not admin or admin.status != "active":
            return None
        if (n - (row.last_seen_at or row.created_at)).total_seconds() > 60:
            row.last_seen_at = n
            s.commit()
        roles = roles_of(admin)
        return {
            "type": "staff_user", "id": admin.admin_id, "name": admin.name, "email": admin.email,
            "roles": roles, "permissions": sorted(perms.permissions_for(roles)),
            "mustChangePassword": bool(admin.must_change_password), "sessionId": row.session_id,
        }
    finally:
        s.close()


# ---------------------------------------------------------------- decorators
def require_staff(permission=None, allow_password_change=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            actor = resolve_staff_actor(request.headers.get("Staff-Token"))
            if not actor:
                return jsonify(error="Unauthorized"), 401
            if actor["mustChangePassword"] and not allow_password_change:
                return jsonify(error="You must change your password first", mustChangePassword=True), 403
            if permission and permission not in actor["permissions"]:
                _audit_now({"type": "staff_user", "id": actor["id"], "name": actor["name"]},
                           "staff.permission_denied", target=request.path, details={"needed": permission})
                return jsonify(error="You do not have permission to do this"), 403
            g.actor = actor
            return fn(*a, **k)
        return wrapper
    return deco


def require_master(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not _ctx["is_master_key"]():
            return jsonify(error="Unauthorized: master key required"), 401
        g.actor = {"type": "master_key", "id": None, "name": "Master key"}
        return fn(*a, **k)
    return wrapper


def require_access_reason(resource):
    """For routes that expose customer personal data. The caller must send
    X-Access-Reason; the access itself is written to the audit log."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            c = cfg()
            if not c["reason_enforced"]:
                return fn(*a, **k)
            reason = (request.headers.get("X-Access-Reason") or "").strip()[:500]
            if len(reason) < c["reason_min"]:
                return jsonify(error=f"A reason of at least {c['reason_min']} characters is required "
                                     f"(send it in the X-Access-Reason header)", reasonRequired=True), 400
            actor = getattr(g, "actor", None) or {"type": "unknown", "id": None, "name": "Unknown"}
            _audit_now(actor, "sensitive.access", target=f"{resource}:{request.path}", reason=reason)
            return fn(*a, **k)
        return wrapper
    return deco


# ---------------------------------------------------------------- views
def _user_dict(a):
    return {
        "adminId": a.admin_id, "name": a.name, "email": a.email, "status": a.status,
        "roles": roles_of(a), "totpEnabled": bool(a.totp_enabled),
        "locked": _is_locked(a), "mustChangePassword": bool(a.must_change_password),
        "createdAt": a.created_at.isoformat() + "Z" if a.created_at else None,
    }


def _me(actor):
    return {k: actor[k] for k in ("id", "name", "email", "roles", "permissions", "mustChangePassword")}


def _active_admin_count(s):
    return sum(1 for u in s.query(AdminUser).filter(AdminUser.status == "active").all() if "admin" in roles_of(u))


# ================================================================= routes
def init_staff_auth(app, deps):
    """deps: Session, rate_limit, get_client_ip, send_email, is_master_key."""
    _ctx.update(deps)
    rl = deps["rate_limit"]

    if not os.environ.get("STAFF_TOTP_ENC_KEY"):
        print("WARNING: STAFF_TOTP_ENC_KEY is not set. Staff 2FA enrolment will refuse to run "
              "(fail closed) until it is. Generate one with: "
              "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")

    def need_db():
        if not _S():
            return jsonify(error="Database not configured"), 500
        return None

    # ---------- bootstrap (master key, only while no active Admin exists)
    @app.route("/api/staff/bootstrap-admin", methods=["POST"])
    @rl(5, 600)
    @require_master
    def staff_bootstrap_admin():
        e = need_db()
        if e:
            return e
        body = _body()
        name = (body.get("name") or "").strip()[:255]
        email = (body.get("email") or "").strip().lower()[:255]
        pw = body.get("password") or ""
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify(error="name and a valid email are required"), 400
        problem = _password_problem(pw, email)
        if problem:
            return jsonify(error=problem), 400
        s = _S()()
        try:
            if _active_admin_count(s) > 0:
                return jsonify(error="An active Admin already exists. Use the staff portal to manage accounts."), 409
            admin = s.query(AdminUser).filter(func.lower(AdminUser.email) == email).first()
            created = admin is None
            if created:
                admin = AdminUser(admin_id=_gen_admin_id(), name=name, email=email, status="active")
                s.add(admin)
            admin.name = name
            admin.password_hash = generate_password_hash(pw)
            admin.status = "active"
            admin.roles = ["staff", "admin"]
            admin.must_change_password = False
            admin.password_changed_at = now()
            admin.failed_attempts = 0
            admin.locked_until = None
            admin.totp_enabled = False
            admin.totp_secret_enc = None
            admin.recovery_codes = []
            _revoke_all_sessions(s, admin.admin_id)
            write_audit(s, g.actor, "staff.bootstrap_admin", target=admin.admin_id,
                        details={"created": created, "email": email})
            s.commit()
            return jsonify(success=True, created=created, admin=_user_dict(admin)), 201
        finally:
            s.close()

    # ---------- step 1: password
    @app.route("/api/staff/login", methods=["POST"])
    @rl(10, 600)
    def staff_login():
        e = need_db()
        if e:
            return e
        body = _body()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if not email or not password or len(email) > 255 or len(password) > 256:
            return jsonify(error="email and password are required"), 400
        s = _S()()
        try:
            admin = s.query(AdminUser).filter(func.lower(AdminUser.email) == email).first()
            locked = bool(admin and _is_locked(admin))
            pw_ok = check_password_hash(admin.password_hash if admin else _DUMMY_HASH, password)
            if not admin or not pw_ok or locked or admin.status != "active":
                why = "unknown" if not admin else "locked" if locked else "disabled" if admin.status != "active" else "bad_password"
                if admin and admin.status == "active" and not locked and not pw_ok:
                    just_locked = _register_failure(admin)
                    if just_locked:
                        why = "bad_password_now_locked"
                write_audit(s, {"type": "anonymous", "id": admin.admin_id if admin else None, "name": email[:200]},
                            "staff.login.failed", target=admin.admin_id if admin else None, details={"why": why})
                s.commit()
                return jsonify(error="Invalid email or password, or the account is temporarily locked"), 401

            roles = roles_of(admin)
            reason = None
            if "admin" in roles:
                reason, err = _need_reason(body)
                if err:
                    return err
            step_kind = "challenge_totp" if admin.totp_enabled else "challenge_enroll"
            token = _new_session_row(s, admin, step_kind, minutes=cfg()["challenge_minutes"], reason=reason)
            s.commit()
            return jsonify(success=True, step="totp" if admin.totp_enabled else "enroll",
                           challenge=token, mustChangePassword=bool(admin.must_change_password))
        finally:
            s.close()

    def _finish_login(s, admin, challenge_row, extra_audit=None):
        challenge_row.revoked_at = now()
        admin.failed_attempts = 0
        admin.locked_until = None
        token = _new_session_row(s, admin, "session", hours=cfg()["absolute_hours"])
        write_audit(s, _actor_of(admin), "staff.login.success", target=admin.admin_id,
                    details=extra_audit, reason=challenge_row.reason)
        roles = roles_of(admin)
        me = {"id": admin.admin_id, "name": admin.name, "email": admin.email, "roles": roles,
              "permissions": sorted(perms.permissions_for(roles)),
              "mustChangePassword": bool(admin.must_change_password)}
        return token, me

    # ---------- step 2: authenticator or recovery code
    @app.route("/api/staff/login/verify", methods=["POST"])
    @rl(10, 600)
    def staff_login_verify():
        e = need_db()
        if e:
            return e
        body = _body()
        s = _S()()
        try:
            row, admin = _get_challenge(s, body.get("challenge"), "challenge_totp")
            if not row:
                return jsonify(error="Sign-in expired. Start again."), 401
            if not _second_factor_ok(admin, body.get("code")):
                locked = _register_failure(admin)
                write_audit(s, _actor_of(admin), "staff.login.failed", target=admin.admin_id,
                            details={"why": "bad_second_factor_now_locked" if locked else "bad_second_factor"})
                if locked:
                    _revoke_all_sessions(s, admin.admin_id)
                s.commit()
                return jsonify(error="Invalid code"), 401
            token, me = _finish_login(s, admin, row)
            s.commit()
            return jsonify(success=True, token=token, user=me)
        finally:
            s.close()

    # ---------- first-time 2FA enrolment
    @app.route("/api/staff/2fa/enroll/start", methods=["POST"])
    @rl(10, 600)
    def staff_enroll_start():
        e = need_db()
        if e:
            return e
        if not _fernet():
            return jsonify(error="Two-step sign-in is not configured on the server (STAFF_TOTP_ENC_KEY). "
                                 "Contact the system owner."), 503
        body = _body()
        s = _S()()
        try:
            row, admin = _get_challenge(s, body.get("challenge"), "challenge_enroll")
            if not row:
                return jsonify(error="Sign-in expired. Start again."), 401
            secret = totp.new_secret()
            admin.totp_secret_enc = _encrypt(secret)
            admin.totp_enabled = False
            s.commit()
            return jsonify(success=True, secret=secret,
                           otpauthUri=totp.provisioning_uri(secret, admin.email, "Tripbookerz Staff"))
        finally:
            s.close()

    @app.route("/api/staff/2fa/enroll/confirm", methods=["POST"])
    @rl(10, 600)
    def staff_enroll_confirm():
        e = need_db()
        if e:
            return e
        body = _body()
        s = _S()()
        try:
            row, admin = _get_challenge(s, body.get("challenge"), "challenge_enroll")
            if not row:
                return jsonify(error="Sign-in expired. Start again."), 401
            if not admin.totp_secret_enc or not _totp_verify_and_consume(admin, (body.get("code") or "").strip()):
                if _register_failure(admin):
                    _revoke_all_sessions(s, admin.admin_id)
                s.commit()
                return jsonify(error="Invalid code. Check your phone's clock and try again."), 401
            plain, hashed = _make_recovery_codes()
            admin.totp_enabled = True
            admin.recovery_codes = hashed
            write_audit(s, _actor_of(admin), "staff.2fa.enabled", target=admin.admin_id)
            token, me = _finish_login(s, admin, row, extra_audit={"enrolled_2fa": True})
            s.commit()
            return jsonify(success=True, token=token, user=me, recoveryCodes=plain,
                           note="Save these recovery codes now. They are shown only once; each works once.")
        finally:
            s.close()

    # ---------- session
    @app.route("/api/staff/me", methods=["GET"])
    @require_staff(allow_password_change=True)
    def staff_me():
        return jsonify(user=_me(g.actor))

    @app.route("/api/staff/logout", methods=["POST"])
    @require_staff(allow_password_change=True)
    def staff_logout():
        s = _S()()
        try:
            row = s.get(StaffSession, g.actor["sessionId"])
            if row:
                row.revoked_at = now()
            write_audit(s, g.actor, "staff.logout", target=g.actor["id"])
            s.commit()
            return jsonify(success=True)
        finally:
            s.close()

    @app.route("/api/staff/logout-all", methods=["POST"])
    @require_staff(allow_password_change=True)
    def staff_logout_all():
        s = _S()()
        try:
            _revoke_all_sessions(s, g.actor["id"])
            write_audit(s, g.actor, "staff.logout_all", target=g.actor["id"])
            s.commit()
            return jsonify(success=True)
        finally:
            s.close()

    # ---------- password change / reset
    @app.route("/api/staff/password/change", methods=["POST"])
    @rl(10, 600)
    @require_staff(allow_password_change=True)
    def staff_password_change():
        body = _body()
        cur, new = body.get("currentPassword") or "", body.get("newPassword") or ""
        s = _S()()
        try:
            admin = s.get(AdminUser, g.actor["id"])
            if not check_password_hash(admin.password_hash, cur):
                if _register_failure(admin):
                    _revoke_all_sessions(s, admin.admin_id)
                s.commit()
                return jsonify(error="Current password is incorrect"), 401
            problem = _password_problem(new, admin.email)
            if problem:
                return jsonify(error=problem), 400
            if check_password_hash(admin.password_hash, new):
                return jsonify(error="New password must be different from the current one"), 400
            admin.password_hash = generate_password_hash(new)
            admin.must_change_password = False
            admin.password_changed_at = now()
            _revoke_all_sessions(s, admin.admin_id, except_id=g.actor["sessionId"])
            write_audit(s, g.actor, "staff.password.change", target=admin.admin_id)
            s.commit()
            return jsonify(success=True)
        finally:
            s.close()

    @app.route("/api/staff/password/reset/request", methods=["POST"])
    @rl(5, 600)
    def staff_reset_request():
        e = need_db()
        if e:
            return e
        body = _body()
        email = (body.get("email") or "").strip().lower()[:255]
        generic = jsonify(success=True, message="If that account exists, a reset link has been emailed.")
        if not email:
            return generic
        s = _S()()
        try:
            admin = s.query(AdminUser).filter(func.lower(AdminUser.email) == email).first()
            if admin and admin.status == "active":
                token = secrets.token_urlsafe(32)
                s.add(PasswordResetToken(token_hash=th(token), admin_id=admin.admin_id,
                                         expires_at=now() + timedelta(minutes=cfg()["reset_minutes"]),
                                         ip_address=_client_ip()))
                write_audit(s, _actor_of(admin), "staff.password.reset_requested", target=admin.admin_id)
                s.commit()
                # token travels in the URL fragment so it is not sent to servers/logs/referrers
                link = f"{cfg()['portal_url']}/#reset={token}"
                html = (f"<p>Hello {admin.name},</p><p>A password reset was requested for your Tripbookerz staff account. "
                        f"The link works once and expires in {cfg()['reset_minutes']} minutes. You will also need a code "
                        f"from your authenticator app.</p><p><a href=\"{link}\">Reset password</a></p>"
                        f"<p>If you did not request this, ignore this email and tell an Admin.</p>")
                try:
                    ok, _raw = _ctx["send_email"](admin.email, "Reset your Tripbookerz staff password", html)
                    if not ok:
                        print("STAFF RESET EMAIL FAILED: provider returned not-ok")
                except Exception as ex:
                    print(f"STAFF RESET EMAIL FAILED: {type(ex).__name__}")
            else:
                write_audit(s, {"type": "anonymous", "id": None, "name": email[:200]},
                            "staff.password.reset_requested_unknown", details=None)
                s.commit()
            return generic
        finally:
            s.close()

    @app.route("/api/staff/password/reset/confirm", methods=["POST"])
    @rl(10, 600)
    def staff_reset_confirm():
        e = need_db()
        if e:
            return e
        body = _body()
        token = body.get("token")
        bad = (jsonify(error="This reset link is invalid or has expired"), 400)
        if not isinstance(token, str) or len(token) > 200:
            return bad
        s = _S()()
        try:
            row = s.get(PasswordResetToken, th(token))
            if not row or row.used_at or row.expires_at < now():
                return bad
            admin = s.get(AdminUser, row.admin_id)
            if not admin or admin.status != "active" or _is_locked(admin):
                return bad
            problem = _password_problem(body.get("newPassword") or "", admin.email)
            if problem:
                return jsonify(error=problem), 400
            if admin.totp_enabled and not _second_factor_ok(admin, body.get("code")):
                locked = _register_failure(admin)
                write_audit(s, _actor_of(admin), "staff.password.reset_failed", target=admin.admin_id,
                            details={"why": "bad_second_factor_now_locked" if locked else "bad_second_factor"})
                s.commit()
                return jsonify(error="Invalid authenticator or recovery code"), 401
            row.used_at = now()
            admin.password_hash = generate_password_hash(body["newPassword"])
            admin.must_change_password = False
            admin.password_changed_at = now()
            admin.failed_attempts = 0
            _revoke_all_sessions(s, admin.admin_id)
            write_audit(s, _actor_of(admin), "staff.password.reset_done", target=admin.admin_id)
            s.commit()
            return jsonify(success=True)
        finally:
            s.close()

    # ---------- account management (Admin)
    def _target(s, admin_id):
        return s.get(AdminUser, admin_id)

    @app.route("/api/staff/users", methods=["GET"])
    @require_staff("user.assign_role")
    def staff_list_users():
        s = _S()()
        try:
            rows = s.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
            return jsonify([_user_dict(a) for a in rows])
        finally:
            s.close()

    @app.route("/api/staff/users", methods=["POST"])
    @require_staff("user.create")
    def staff_create_user():
        body = _body()
        reason, err = _need_reason(body)
        if err:
            return err
        name = (body.get("name") or "").strip()[:255]
        email = (body.get("email") or "").strip().lower()[:255]
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify(error="name and a valid email are required"), 400
        req_roles = body.get("roles") or ["staff"]
        if not isinstance(req_roles, list) or any(str(r).strip().lower() not in perms.VALID_ROLES for r in req_roles):
            return jsonify(error=f"roles must be a list from: {', '.join(perms.VALID_ROLES)}"), 400
        roles = perms.normalize_roles(req_roles)
        if "admin" in roles and "user.assign_role" not in g.actor["permissions"]:
            return jsonify(error="You may not grant the admin role"), 403
        s = _S()()
        try:
            if s.query(AdminUser).filter(func.lower(AdminUser.email) == email).first():
                return jsonify(error="An account with this email already exists"), 409
            temp = secrets.token_urlsafe(12)
            admin = AdminUser(admin_id=_gen_admin_id(), name=name, email=email,
                              password_hash=generate_password_hash(temp), status="active", roles=roles,
                              must_change_password=True, totp_enabled=False, recovery_codes=[],
                              failed_attempts=0, password_changed_at=now())
            s.add(admin)
            write_audit(s, g.actor, "staff.user.create", target=admin.admin_id,
                        details={"email": email, "roles": roles}, reason=reason)
            s.commit()
            return jsonify(success=True, user=_user_dict(admin), temporaryPassword=temp,
                           note="Give this temporary password to the person directly. It is shown once and must be changed at first sign-in."), 201
        finally:
            s.close()

    def _guard_target(s, admin_id, allow_self=False):
        t = _target(s, admin_id)
        if not t:
            return None, (jsonify(error="Not found"), 404)
        if t.admin_id == g.actor["id"] and not allow_self:
            return None, (jsonify(error="You cannot do this to your own account"), 400)
        return t, None

    @app.route("/api/staff/users/<admin_id>/roles", methods=["POST"])
    @require_staff("user.assign_role")
    def staff_set_roles(admin_id):
        body = _body()
        reason, err = _need_reason(body)
        if err:
            return err
        req_roles = body.get("roles")
        if not isinstance(req_roles, list) or any(str(r).strip().lower() not in perms.VALID_ROLES for r in req_roles):
            return jsonify(error=f"roles must be a list from: {', '.join(perms.VALID_ROLES)}"), 400
        new_roles = perms.normalize_roles(req_roles)
        s = _S()()
        try:
            t, err = _guard_target(s, admin_id)
            if err:
                return err
            old = roles_of(t)
            if "admin" in old and "admin" not in new_roles and t.status == "active" and _active_admin_count(s) <= 1:
                return jsonify(error="Cannot remove the last active Admin"), 409
            t.roles = new_roles
            _revoke_all_sessions(s, t.admin_id)         # permissions changed: force fresh sign-in
            write_audit(s, g.actor, "staff.user.set_roles", target=t.admin_id,
                        details={"from": old, "to": new_roles}, reason=reason)
            s.commit()
            return jsonify(success=True, user=_user_dict(t))
        finally:
            s.close()

    def _set_status(admin_id, status):
        body = _body()
        reason, err = _need_reason(body)
        if err:
            return err
        s = _S()()
        try:
            t, err = _guard_target(s, admin_id)
            if err:
                return err
            if status == "disabled" and "admin" in roles_of(t) and t.status == "active" and _active_admin_count(s) <= 1:
                return jsonify(error="Cannot disable the last active Admin"), 409
            t.status = status
            if status == "disabled":
                _revoke_all_sessions(s, t.admin_id)
                t.session_token = None
            write_audit(s, g.actor, f"staff.user.{'disable' if status == 'disabled' else 'enable'}",
                        target=t.admin_id, reason=reason)
            s.commit()
            return jsonify(success=True, user=_user_dict(t))
        finally:
            s.close()

    @app.route("/api/staff/users/<admin_id>/disable", methods=["POST"])
    @require_staff("user.disable")
    def staff_disable(admin_id):
        return _set_status(admin_id, "disabled")

    @app.route("/api/staff/users/<admin_id>/enable", methods=["POST"])
    @require_staff("user.disable")
    def staff_enable(admin_id):
        return _set_status(admin_id, "active")

    @app.route("/api/staff/users/<admin_id>/reset-2fa", methods=["POST"])
    @require_staff("user.reset_password")
    def staff_reset_2fa(admin_id):
        body = _body()
        reason, err = _need_reason(body)
        if err:
            return err
        s = _S()()
        try:
            t, err = _guard_target(s, admin_id)     # an Admin's own reset needs ANOTHER Admin
            if err:
                return err
            t.totp_secret_enc = None
            t.totp_enabled = False
            t.totp_last_step = None
            t.recovery_codes = []
            _revoke_all_sessions(s, t.admin_id)
            write_audit(s, g.actor, "staff.user.reset_2fa", target=t.admin_id, reason=reason)
            s.commit()
            return jsonify(success=True, user=_user_dict(t),
                           note="They will be asked to set up their authenticator app at next sign-in. Verify their identity first.")
        finally:
            s.close()

    @app.route("/api/staff/users/<admin_id>/reset-password", methods=["POST"])
    @require_staff("user.reset_password")
    def staff_force_reset_password(admin_id):
        body = _body()
        reason, err = _need_reason(body)
        if err:
            return err
        s = _S()()
        try:
            t, err = _guard_target(s, admin_id)
            if err:
                return err
            temp = secrets.token_urlsafe(12)
            t.password_hash = generate_password_hash(temp)
            t.must_change_password = True
            t.password_changed_at = now()
            _revoke_all_sessions(s, t.admin_id)
            write_audit(s, g.actor, "staff.user.force_password_reset", target=t.admin_id, reason=reason)
            s.commit()
            return jsonify(success=True, temporaryPassword=temp,
                           note="Give this to the person directly. Shown once; must be changed at next sign-in.")
        finally:
            s.close()

    @app.route("/api/staff/users/<admin_id>/unlock", methods=["POST"])
    @require_staff("user.reset_password")
    def staff_unlock(admin_id):
        body = _body()
        reason, err = _need_reason(body)
        if err:
            return err
        s = _S()()
        try:
            t, err = _guard_target(s, admin_id, allow_self=True)
            if err:
                return err
            t.locked_until = None
            t.failed_attempts = 0
            write_audit(s, g.actor, "staff.user.unlock", target=t.admin_id, reason=reason)
            s.commit()
            return jsonify(success=True, user=_user_dict(t))
        finally:
            s.close()

    # ---------- audit
    @app.route("/api/staff/audit-log", methods=["GET"])
    @require_staff("audit.view")
    def staff_audit_log():
        action = request.args.get("action")
        actor = request.args.get("actorId")
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 500))
        except ValueError:
            limit = 100
        s = _S()()
        try:
            q = s.query(AuditLog).order_by(AuditLog.created_at.desc())
            if action:
                q = q.filter(AuditLog.action == action)
            if actor:
                q = q.filter(AuditLog.actor_id == actor)
            return jsonify([r.to_dict() for r in q.limit(limit).all()])
        finally:
            s.close()

    @app.route("/api/staff/audit/purge", methods=["POST"])
    @rl(5, 600)
    @require_master
    def staff_audit_purge():
        """Master-key only (deliberately not a staff permission). Deletes audit
        rows past retain_until. dryRun defaults to TRUE: nothing is deleted
        unless the caller sends {"dryRun": false}."""
        e = need_db()
        if e:
            return e
        body = _body()
        dry = body.get("dryRun", True) is not False
        s = _S()()
        try:
            q = s.query(AuditLog).filter(AuditLog.retain_until.isnot(None), AuditLog.retain_until < now())
            n = q.count()
            if not dry and n:
                q.delete(synchronize_session=False)
            write_audit(s, g.actor, "audit.purge", details={"dryRun": dry, "rows": n},
                        reason=_reason_from(body) or None)
            s.commit()
            return jsonify(success=True, dryRun=dry, rowsEligible=n, rowsDeleted=0 if dry else n)
        finally:
            s.close()
