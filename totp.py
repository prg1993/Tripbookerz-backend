"""
RFC 6238 time-based one-time passwords (authenticator-app codes), standard
library only. Written instead of adding a dependency; verified against the
official RFC 6238 Appendix B test vectors in tests/test_staff_auth.py.

Authenticator apps (Google Authenticator, Microsoft Authenticator, Authy,
1Password...) all implement exactly this: HMAC-SHA1, 6 digits, 30s step.
"""
import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def new_secret() -> str:
    """160-bit random secret, base32 (the format authenticator apps expect)."""
    return base64.b32encode(os.urandom(20)).decode("ascii")


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS, algo=hashlib.sha1) -> str:
    key = base64.b32decode(secret_b32.upper(), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), algo).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def code_at(secret_b32: str, unix_time: float, **kw) -> str:
    return _hotp(secret_b32, int(unix_time // STEP_SECONDS), **kw)


def current_step(now: float = None) -> int:
    return int((now if now is not None else time.time()) // STEP_SECONDS)


def match_step(secret_b32: str, code: str, now: float = None, window: int = 1):
    """Returns the matching time-step (int) if `code` is valid within
    +/- `window` steps of now, else None. The caller must remember the step
    and reject any step <= the last accepted one, so a code can't be reused."""
    if not isinstance(code, str) or len(code) != DIGITS or not code.isdigit():
        return None
    cur = current_step(now)
    found = None
    for step in range(cur - window, cur + window + 1):
        # compare every candidate (no early exit) to keep timing uniform
        if hmac.compare_digest(_hotp(secret_b32, step).encode(), code.encode()):
            found = step
    return found


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )
