"""
Shared HMAC-based integrity/authenticity utilities. Two related but
distinct jobs live here:

1. Signed OFFER TOKENS — handed to a client, which holds onto them and
   sends them back later (a flight offer, a cab quote pair). Signed so
   the client can carry the data around but never tamper with what's
   inside it. Originally lived only in flight_providers.py; now shared
   with cab_providers.py too, and any future product line that needs
   the same "search now, verify and book later" pattern.

2. WEBHOOK SIGNATURE VERIFICATION — the reverse direction: authenticating
   data arriving FROM an external system (e.g. a cab supplier's driver-
   dispatch webhook), so a request can't be trusted just because it
   hit the right URL. Uses its OWN separate secret from offer tokens —
   a compromise of one should never affect the other, same principle
   already used elsewhere in this backend (ADMIN_API_KEY vs.
   OFFER_TOKEN_SECRET are already separate for the same reason).
"""

import os
import time
import hmac
import hashlib
import json
import base64
import secrets

# Falls back to a random value if unset, which is fine for a single
# always-running process but means tokens won't survive a restart —
# set OFFER_TOKEN_SECRET for real deployments. Shared by every product
# line that uses this module, not a separate secret per domain.
OFFER_TOKEN_SECRET = os.environ.get("OFFER_TOKEN_SECRET") or secrets.token_hex(32)
OFFER_TOKEN_TTL_SECONDS = 15 * 60  # an offer is only bookable for 15 minutes

# Separate secret for verifying INCOMING webhooks (see module docstring
# for why this isn't the same secret as OFFER_TOKEN_SECRET). A real
# supplier integration would be given this value out-of-band when the
# webhook is configured on their end — same as how Stripe, Razorpay,
# and most real webhook systems work.
CAB_DISPATCH_WEBHOOK_SECRET = os.environ.get("CAB_DISPATCH_WEBHOOK_SECRET") or secrets.token_hex(32)


def make_signed_token(payload_dict):
    """payload_dict can be any JSON-serializable dict — this module
    doesn't know or care whether it's a flight offer, a cab quote pair,
    or anything else. issuedAt is added automatically."""
    payload = dict(payload_dict)
    payload["issuedAt"] = int(time.time())
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(OFFER_TOKEN_SECRET.encode(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(sig).decode()


def verify_signed_token(token):
    """Returns (ok, payload_or_error). Checks the HMAC signature (so the
    token can't have been tampered with client-side) and expiry (so a
    stale offer from an hour ago can't be booked at a price nobody
    re-validated)."""
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64.encode())
        sig = base64.urlsafe_b64decode(sig_b64.encode())
    except Exception:
        return False, "Malformed offer token"

    expected_sig = hmac.new(OFFER_TOKEN_SECRET.encode(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return False, "Offer token signature is invalid"

    try:
        payload = json.loads(raw)
    except Exception:
        return False, "Malformed offer token payload"

    if time.time() - payload.get("issuedAt", 0) > OFFER_TOKEN_TTL_SECONDS:
        return False, "This offer has expired — please search again"

    return True, payload


def compute_webhook_signature(raw_body_bytes):
    """The value a genuine sender must compute and attach to a webhook
    request — an HMAC-SHA256 over the exact raw request body, hex-
    encoded. Operates on RAW BYTES, not a parsed/re-serialized dict,
    because signing a re-serialized copy would let two byte-for-byte
    different payloads produce the same signature if they happen to
    parse to the same dict — the signature has to cover exactly what
    was actually sent, not a reinterpretation of it."""
    return hmac.new(CAB_DISPATCH_WEBHOOK_SECRET.encode(), raw_body_bytes, hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body_bytes, provided_signature):
    """True only if provided_signature is the exact, valid signature for
    this exact body. A missing signature is never treated as valid —
    there is no 'unsigned but trusted' path."""
    if not provided_signature:
        return False
    expected = compute_webhook_signature(raw_body_bytes)
    return hmac.compare_digest(expected, provided_signature)
