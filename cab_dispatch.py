"""
Phase 2: real-time driver dispatch notification pipeline for cab
transfers booked as part of a holiday package (Phase 1, cab_providers.py,
covers quoting/selecting/booking the cab itself — this covers what
happens between booking and the actual pickup).

HONEST STATE TODAY: no real cab supplier exists to ever call the real
webhook this powers (see app.py's /webhooks/cab-dispatch), so a
"simulate dispatch" admin endpoint exists specifically to exercise this
whole pipeline end-to-end without one. WhatsApp is a mock provider —
no real WhatsApp Business API account exists either.

WHY THIS ISN'T A CIRCULAR IMPORT: this module needs to send email as
part of the notification pipeline, but the real Resend integration
lives in app.py. Rather than import app.py from here (backwards — a
lower-level module importing the thing that imports it), the email-
sending FUNCTION is passed in as a parameter by whoever calls
notify_driver_dispatched() below. app.py, which already has
send_email_via_provider(), passes that in. This module never needs to
know anything about which email provider is actually configured.
"""

import os
import time
import random
import secrets
from datetime import datetime

from models import CabDispatch, Booking

# ---------- WhatsApp provider dispatch — mirrors OTP_PROVIDER /
# EMAIL_PROVIDER / PAYMENT_PROVIDER exactly, same reasoning each time:
# one dispatch table, one honest mock today, a real provider later is
# one adapter function + one registry line, not a rewrite. ----------

def mock_send_whatsapp(to_phone, message):
    """Returns (success: bool, raw: dict). Simulates sending a WhatsApp
    message — NOT a real WhatsApp Business API call. No real account
    exists yet."""
    time.sleep(random.uniform(0.1, 0.3))
    return True, {
        "to": to_phone, "message": message, "simulated": True,
        "messageId": "wa_mock_" + secrets.token_hex(6),
        "note": "No real WhatsApp Business API is integrated yet.",
    }


WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "mock").lower()
_WHATSAPP_PROVIDERS = {"mock": mock_send_whatsapp}
_WHATSAPP_PROVIDERS_IS_MOCK = {"mock": True}
if WHATSAPP_PROVIDER not in _WHATSAPP_PROVIDERS:
    print(f"WARNING: WHATSAPP_PROVIDER='{WHATSAPP_PROVIDER}' is not recognized (expected one of: "
          f"{', '.join(_WHATSAPP_PROVIDERS.keys())}). Falling back to 'mock'.")
    WHATSAPP_PROVIDER = "mock"


def send_whatsapp_via_provider(to_phone, message):
    return _WHATSAPP_PROVIDERS[WHATSAPP_PROVIDER](to_phone, message)


# ---------- Mock supplier-side behaviors ----------
# These simulate what a REAL supplier's systems would do — assigning
# a driver and eventually calling our webhook, or responding to a
# polling request. Used by the admin simulate-dispatch endpoint and
# the safety-net job respectively, so both are actually exercisable.

DRIVER_NAME_POOL = ["Rajesh Sharma", "Suresh Patil", "Vikram Singh", "Anil Kumar", "Manoj Yadav", "Deepak Rao"]
VEHICLE_MODEL_BY_CATEGORY = {
    "SEDAN": ["Swift Dzire", "Honda Amaze", "Hyundai Aura"],
    "SUV": ["Toyota Innova Crysta", "Maruti Ertiga", "Mahindra Marazzo"],
    "TEMPO_TRAVELLER": ["Force Traveller"],
}
VEHICLE_COLORS = ["White", "Silver", "Grey"]


def generate_mock_dispatch_event(booking_ref, category):
    """Simulates the payload a real supplier's webhook would send. Used
    by the admin simulate-dispatch endpoint — NOT a real dispatch."""
    rnd = random.Random(hash((booking_ref, "dispatch")))
    model = rnd.choice(VEHICLE_MODEL_BY_CATEGORY.get(category, VEHICLE_MODEL_BY_CATEGORY["SEDAN"]))
    reg_state = rnd.choice(["DL", "MH", "KA", "GA"])
    return {
        "booking_ref": booking_ref,
        "event_type": "DRIVER_DISPATCHED",
        "vehicle": {
            "model": model,
            "registration_number": f"{reg_state}-{rnd.randint(1,99):02d}-{chr(65+rnd.randint(0,25))}{chr(65+rnd.randint(0,25))}-{rnd.randint(1000,9999)}",
            "color": rnd.choice(VEHICLE_COLORS),
        },
        "driver": {
            "name": rnd.choice(DRIVER_NAME_POOL),
            "phone": "+91" + str(rnd.randint(7000000000, 9999999999)),
            "rating": round(rnd.uniform(4.2, 5.0), 1),
        },
        "live_tracking_url": f"https://track.cabprovider.example/trip/TR-{secrets.token_hex(4).upper()}",
    }


def mock_poll_supplier_for_driver(booking_ref, category):
    """Simulates calling a supplier's API to check whether a driver has
    been assigned yet — used by the safety-net job. Sometimes still
    empty (simulating a genuinely late dispatch), matching the real-
    world reason this safety net needs to exist at all."""
    time.sleep(random.uniform(0.1, 0.3))
    if random.random() < 0.6:
        return True, generate_mock_dispatch_event(booking_ref, category)
    return False, None


# ---------- Notification orchestration ----------

def notify_driver_dispatched(dispatch_event, traveler_phone, traveler_email, send_email_fn):
    """Fires WhatsApp AND email — the source spec's own diagram shows
    these as parallel branches converging back together, not a
    fallback-only-if-the-first-fails chain, so both are always
    attempted regardless of whether the other succeeds. send_email_fn
    is provided by the caller (see module docstring) — this function
    never imports app.py directly.

    Returns a dict describing exactly what happened on each channel,
    so the caller can persist accurate per-channel status rather than
    a single ambiguous "notified: true/false"."""
    driver = dispatch_event["driver"]
    vehicle = dispatch_event["vehicle"]
    message = (
        f"Your driver {driver['name']} ({driver['rating']}★) has been assigned for your transfer. "
        f"Vehicle: {vehicle['color']} {vehicle['model']} ({vehicle['registration_number']}). "
        f"Driver contact: {driver['phone']}. Track live: {dispatch_event['live_tracking_url']}"
    )
    wa_ok, wa_raw = send_whatsapp_via_provider(traveler_phone, message) if traveler_phone else (False, {"error": "no phone on file"})

    email_ok, email_raw = (False, {"error": "no email on file"})
    if traveler_email:
        email_html = (
            f"<p>Your driver <b>{driver['name']}</b> ({driver['rating']}★) has been assigned for your transfer.</p>"
            f"<p>Vehicle: {vehicle['color']} {vehicle['model']} ({vehicle['registration_number']})<br>"
            f"Driver contact: {driver['phone']}<br>"
            f"<a href=\"{dispatch_event['live_tracking_url']}\">Track your ride live</a></p>"
        )
        email_ok, email_raw = send_email_fn(traveler_email, "Your driver is on the way", email_html)

    return {
        "whatsappSent": wa_ok, "whatsappRaw": wa_raw,
        "emailSent": email_ok, "emailRaw": email_raw,
    }


def process_dispatch_event(session, dispatch_event, send_email_fn):
    """THE shared processing logic for a driver-dispatch event, used by
    BOTH the real webhook receiver (app.py's /webhooks/cab-dispatch,
    after it's verified the signature) and the admin simulate-dispatch
    endpoint — one implementation, not two routes each re-implementing
    'look up the booking, upsert the dispatch row, notify, record the
    outcome' slightly differently.

    Returns (ok, result_or_error). The caller (a Flask route) owns
    turning this into an HTTP response — this function has no
    knowledge of Flask at all, staying testable on its own."""
    booking_ref = dispatch_event.get("booking_ref")
    if not booking_ref:
        return False, "dispatch event is missing booking_ref"

    booking = session.query(Booking).filter(Booking.booking_id == booking_ref).first()
    if not booking:
        return False, f"No booking found for booking_ref '{booking_ref}'"

    dispatch = session.query(CabDispatch).filter(CabDispatch.booking_id == booking_ref).first()
    if not dispatch:
        dispatch = CabDispatch(booking_id=booking_ref)
        session.add(dispatch)

    driver = dispatch_event["driver"]
    vehicle = dispatch_event["vehicle"]
    dispatch.driver_name = driver["name"]
    dispatch.driver_phone = driver["phone"]
    dispatch.driver_rating = driver["rating"]
    dispatch.vehicle_model = vehicle["model"]
    dispatch.vehicle_registration = vehicle["registration_number"]
    dispatch.vehicle_color = vehicle["color"]
    dispatch.live_tracking_url = dispatch_event["live_tracking_url"]
    dispatch.dispatch_received_at = datetime.utcnow()

    notify_result = notify_driver_dispatched(dispatch_event, booking.traveler_phone, booking.traveler_email, send_email_fn)
    now = datetime.utcnow()
    if notify_result["whatsappSent"]:
        dispatch.whatsapp_sent = True
        dispatch.whatsapp_sent_at = now
    if notify_result["emailSent"]:
        dispatch.email_sent = True
        dispatch.email_sent_at = now

    session.commit()
    return True, {"dispatch": dispatch.to_dict(), "notify": notify_result}


def run_safety_net_check(session, pending_dispatches, send_email_fn):
    """THE T-90-MINUTE SAFETY NET's actual logic, separated from the
    Flask route that fetches pending_dispatches — that query needs real
    SQLAlchemy comparison operators this module has no reason to know
    about; this function just needs a plain list of dispatch rows that
    still have no driver assigned within their pickup window, however
    the caller found them.

    For each: polls the (mocked) supplier. If a driver is found, routes
    through the exact same process_dispatch_event() a real webhook
    would use — one processing path, not two. If the poll still comes
    back empty, escalates (marks it, logs an alert — see this
    function's own note on why that's a log line and not a real page/
    ticket, since no real ops system exists to page).

    Returns {checked, resolved, escalated} — counts, for the caller to
    report back however it likes (an HTTP response, a log line, etc.)."""
    from datetime import datetime
    checked, resolved, escalated = 0, 0, 0
    for dispatch in pending_dispatches:
        checked += 1
        dispatch.safety_net_last_checked_at = datetime.utcnow()
        available, event = mock_poll_supplier_for_driver(dispatch.booking_id, dispatch.category)
        if available:
            ok, _ = process_dispatch_event(session, event, send_email_fn)
            if ok:
                resolved += 1
                continue
        dispatch.escalated_to_ops = True
        escalated += 1
        print(f"ALERT: cab dispatch safety net escalation — booking {dispatch.booking_id} still has no driver within its pickup window. Manual ops attention needed.")
    session.commit()
    return {"checked": checked, "resolved": resolved, "escalated": escalated}
