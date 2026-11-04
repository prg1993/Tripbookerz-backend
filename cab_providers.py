"""
Multi-supplier cab/transfer fare aggregation, built from the same
architectural principle as flight_providers.py: this file owns
everything supplier-specific, so app.py never talks to a specific
supplier directly, and adding a real one later means editing this
file only.

HONEST STATE TODAY: all three suppliers below (Savaari, TBO Transfers,
Goibibo/Local) are MOCKS. No real accounts or API keys exist for any
of them. The Fulfillment Reliability Scores are the source document's
own stated/implied numbers (Savaari 98%, a "Secondary Aggregator" at
82% — assigned here to Goibibo/Local, with TBO given a reasonable
middle value), not measured from anything real.

REDIS, NOT USED: the source specification calls for caching the
fallback supplier in Redis. This deployment has no Redis and doesn't
need one — the fallback is stored in Postgres (CabProviderLog) instead,
scoped to the same short search-to-booking window flight offer tokens
already use. See the backend README for this decision.

TO ADD A REAL SUPPLIER LATER:
  1. Write one function matching search_cabs(pickup, drop, pickup_dt,
     passengers, luggage) -> list[dict], returning raw quotes in that
     supplier's OWN category names (see any existing _mock_* function).
  2. Write a normalizer entry in SUPPLIER_CATEGORY_MAP translating that
     supplier's raw category strings to the standard taxonomy below.
  3. Add one line each to CAB_SUPPLIERS and SUPPLIER_RELIABILITY.
  4. Set that supplier's real credentials as their own env vars.
Nothing else changes — the aggregation, normalization, routing, and
fallback logic are all supplier-agnostic.
"""

import os
import time
import random
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from offer_tokens import make_signed_token, verify_signed_token

# ---------- Standard vehicle taxonomy ----------
# The source spec's own table stops at SUV (6 pax / 4 bags). Added
# TEMPO_TRAVELLER for larger groups (common real category for Indian
# tour groups, 10-12 pax) since the spec gives no guidance for groups
# beyond SUV capacity and no multi-vehicle-split logic — this is one
# reasonable extension, not an attempt to solve the harder "split a
# group across multiple cabs" problem, which the spec doesn't address
# at all and isn't being built here.
VEHICLE_CATEGORIES = {
    "SEDAN": {"max_pax": 3, "max_bags": 2, "label": "Sedan"},
    "SUV": {"max_pax": 6, "max_bags": 4, "label": "SUV"},
    "TEMPO_TRAVELLER": {"max_pax": 12, "max_bags": 10, "label": "Tempo Traveller"},
}
CATEGORY_ORDER = ["SEDAN", "SUV", "TEMPO_TRAVELLER"]  # smallest to largest, for upgrade/downgrade comparisons


def default_category_for(passengers, luggage_count=0):
    """Smallest category that actually fits the group — the capacity
    floor. Also returns whether a luggage-driven upgrade should be
    PROMPTED (never forced) — mirrors the spec's own example (2
    passengers with >2 large bags get a suggested SUV upgrade)."""
    chosen = None
    for cat in CATEGORY_ORDER:
        info = VEHICLE_CATEGORIES[cat]
        if passengers <= info["max_pax"]:
            chosen = cat
            break
    if chosen is None:
        chosen = CATEGORY_ORDER[-1]  # group exceeds even the largest category — flagged by the caller, not silently capped

    suggest_upgrade = False
    if chosen and luggage_count > VEHICLE_CATEGORIES[chosen]["max_bags"]:
        idx = CATEGORY_ORDER.index(chosen)
        if idx + 1 < len(CATEGORY_ORDER):
            suggest_upgrade = CATEGORY_ORDER[idx + 1]
    return chosen, suggest_upgrade


def validate_category_choice(requested_category, passengers):
    """CAPACITY FLOOR ENFORCEMENT (the spec's own term) — a customer
    can upgrade to any category that fits, but can never choose one
    smaller than the group's minimum requirement. Returns (ok, error)."""
    if requested_category not in VEHICLE_CATEGORIES:
        return False, f"Unknown vehicle category '{requested_category}'"
    if passengers > VEHICLE_CATEGORIES[requested_category]["max_pax"]:
        return False, (
            f"{VEHICLE_CATEGORIES[requested_category]['label']} seats up to "
            f"{VEHICLE_CATEGORIES[requested_category]['max_pax']} passengers — "
            f"{passengers} passengers need at least the next category up."
        )
    return True, None


# ---------- Configuration ----------
CAB_SUPPLIERS_ENABLED = [
    p.strip() for p in os.environ.get(
        "CAB_SUPPLIERS_ENABLED", "savaari,tbo_transfers,goibibo_local"
    ).split(",") if p.strip()
]
CAB_SUPPLIER_TIMEOUT_SECONDS = float(os.environ.get("CAB_SUPPLIER_TIMEOUT_SECONDS", "1.8"))  # 1,800ms, per the spec
GST_RATE = 0.05  # 5%, per the spec — verified against the spec's own worked example (Savaari SUV: 1800+250+0, GST=102.50)

# Within this cost delta, a higher-reliability supplier wins over a
# cheaper one — the spec's own SLA/Quality Override rule.
RELIABILITY_OVERRIDE_THRESHOLD = 0.04  # 4%

# Fulfillment Reliability Scores — the spec's own example (Savaari 98%,
# a "Secondary Aggregator" 82%) plus a reasonable middle value for the
# one supplier the spec doesn't score directly. NOT measured from
# anything real — there is no real fulfillment history yet.
SUPPLIER_RELIABILITY = {
    "savaari": 0.98,
    "tbo_transfers": 0.91,
    "goibibo_local": 0.82,
}


def _landed_cost(base_fare, driver_allowance, tolls_permits, commission_or_cashback=0.0):
    """THE NORMALIZATION FORMULA. Verified byte-exact against the
    spec's own worked example before this was trusted: GST is 5% of
    (base_fare + tolls_permits + driver_allowance) — the spec's prose
    doesn't say what GST applies to, but its own numbers resolve that
    unambiguously (1800+250+0=2050; 5% of 2050=102.50, matching the
    spec's own stated GST and total exactly)."""
    gst_base = base_fare + tolls_permits + driver_allowance
    gst = round(gst_base * GST_RATE, 2)
    total = round(base_fare + driver_allowance + tolls_permits + gst - commission_or_cashback, 2)
    return gst, total


def _mock_latency(min_ms, max_ms):
    time.sleep(random.uniform(min_ms, max_ms) / 1000.0)


# ---------- Supplier adapters (all mocked — see module docstring) ----------
# Each returns RAW quotes in that supplier's OWN category naming
# (matching the spec's own table), NOT yet normalized — normalization
# happens once, centrally, in aggregate_cab_quotes() below.

def _savaari_search(pickup, drop, pickup_dt, passengers, luggage):
    _mock_latency(200, 900)
    seed = hash((pickup, drop, pickup_dt, "savaari"))
    rnd = random.Random(seed)
    return [
        {"supplier": "savaari", "supplier_offer_id": f"sav_{secrets.token_hex(6)}",
         "raw_category": "Ac Sedan (Dzire / Etios)", "vehicle_model": "Swift Dzire",
         "base_fare": round(rnd.uniform(1400, 2000), 2), "driver_allowance": 0.0,
         "tolls_permits_est": round(rnd.uniform(100, 300), 2), "commission_or_cashback": 0.0},
        {"supplier": "savaari", "supplier_offer_id": f"sav_{secrets.token_hex(6)}",
         "raw_category": "Ertiga / Innova Crysta", "vehicle_model": "Toyota Innova Crysta",
         "base_fare": round(rnd.uniform(1800, 2600), 2), "driver_allowance": 0.0,
         "tolls_permits_est": round(rnd.uniform(200, 400), 2), "commission_or_cashback": 0.0},
    ]


def _tbo_transfers_search(pickup, drop, pickup_dt, passengers, luggage):
    _mock_latency(250, 1100)
    seed = hash((pickup, drop, pickup_dt, "tbo_transfers"))
    rnd = random.Random(seed)
    return [
        {"supplier": "tbo_transfers", "supplier_offer_id": f"tbo_{secrets.token_hex(6)}",
         "raw_category": "Standard Sedan AC", "vehicle_model": "Honda Amaze",
         "base_fare": round(rnd.uniform(1300, 1900), 2), "driver_allowance": round(rnd.uniform(0, 150), 2),
         "tolls_permits_est": round(rnd.uniform(100, 280), 2), "commission_or_cashback": round(rnd.uniform(0, 100), 2)},
        {"supplier": "tbo_transfers", "supplier_offer_id": f"tbo_{secrets.token_hex(6)}",
         "raw_category": "Minivan / SUV Comfort", "vehicle_model": "Maruti Ertiga",
         "base_fare": round(rnd.uniform(1700, 2400), 2), "driver_allowance": round(rnd.uniform(0, 150), 2),
         "tolls_permits_est": round(rnd.uniform(180, 380), 2), "commission_or_cashback": round(rnd.uniform(0, 100), 2)},
    ]


def _goibibo_local_search(pickup, drop, pickup_dt, passengers, luggage):
    # "Goibibo / Local" per the spec's own architecture diagram — a
    # lower-reliability, typically cheaper aggregator/local-operator
    # channel, reflected in both its pricing and its FRS above.
    _mock_latency(300, 1300)
    seed = hash((pickup, drop, pickup_dt, "goibibo_local"))
    rnd = random.Random(seed)
    return [
        {"supplier": "goibibo_local", "supplier_offer_id": f"gbl_{secrets.token_hex(6)}",
         "raw_category": "Sedan", "vehicle_model": "Hyundai Aura",
         "base_fare": round(rnd.uniform(1100, 1700), 2), "driver_allowance": 0.0,
         "tolls_permits_est": round(rnd.uniform(80, 250), 2), "commission_or_cashback": 0.0},
        {"supplier": "goibibo_local", "supplier_offer_id": f"gbl_{secrets.token_hex(6)}",
         "raw_category": "SUV", "vehicle_model": "Mahindra Marazzo",
         "base_fare": round(rnd.uniform(1500, 2200), 2), "driver_allowance": 0.0,
         "tolls_permits_est": round(rnd.uniform(150, 350), 2), "commission_or_cashback": 0.0},
        {"supplier": "goibibo_local", "supplier_offer_id": f"gbl_{secrets.token_hex(6)}",
         "raw_category": "Tempo Traveller 12-seater", "vehicle_model": "Force Traveller",
         "base_fare": round(rnd.uniform(3200, 4500), 2), "driver_allowance": round(rnd.uniform(100, 300), 2),
         "tolls_permits_est": round(rnd.uniform(250, 500), 2), "commission_or_cashback": 0.0},
    ]


CAB_SUPPLIERS = {
    "savaari": _savaari_search,
    "tbo_transfers": _tbo_transfers_search,
    "goibibo_local": _goibibo_local_search,
}

# Which registered suppliers are mocks vs real — mirrors
# FLIGHT_PROVIDERS_IS_MOCK exactly, same reason: this is what
# app.py's production-safety check (enforce_no_mock_providers_in_production)
# checks before allowing a production deploy to silently serve fake
# cab quotes.
CAB_SUPPLIERS_IS_MOCK = {
    "savaari": True,
    "tbo_transfers": True,
    "goibibo_local": True,
}

# Maps each supplier's own raw category string to the standard taxonomy.
# Centralizing this here (rather than each supplier normalizing itself)
# means the taxonomy can change in one place without touching supplier code.
SUPPLIER_CATEGORY_MAP = {
    "savaari": {"Ac Sedan (Dzire / Etios)": "SEDAN", "Ertiga / Innova Crysta": "SUV"},
    "tbo_transfers": {"Standard Sedan AC": "SEDAN", "Minivan / SUV Comfort": "SUV"},
    "goibibo_local": {"Sedan": "SEDAN", "SUV": "SUV", "Tempo Traveller 12-seater": "TEMPO_TRAVELLER"},
}


def _normalize_quote(raw):
    category = SUPPLIER_CATEGORY_MAP.get(raw["supplier"], {}).get(raw["raw_category"])
    if category is None:
        return None  # an unrecognized raw category is dropped, not guessed at
    gst, total = _landed_cost(raw["base_fare"], raw["driver_allowance"], raw["tolls_permits_est"], raw["commission_or_cashback"])
    return {
        "supplier": raw["supplier"], "supplierOfferId": raw["supplier_offer_id"],
        "category": category, "vehicleModel": raw["vehicle_model"],
        "baseFare": raw["base_fare"], "driverAllowance": raw["driver_allowance"],
        "tollsPermitsEst": raw["tolls_permits_est"], "commissionOrCashback": raw["commission_or_cashback"],
        "gstTax": gst, "totalLandedCost": total,
        "reliabilityScore": SUPPLIER_RELIABILITY.get(raw["supplier"], 0.0),
    }


def aggregate_cab_quotes(pickup, drop, pickup_dt, passengers, luggage, requested_category=None):
    """Queries every enabled supplier in parallel (1,800ms timeout, per
    the spec), normalizes every quote to the standard taxonomy, and for
    EACH category present, selects the routed supplier using the same
    two-step rule as the spec: cheapest landed cost wins UNLESS a
    higher-reliability supplier is within RELIABILITY_OVERRIDE_THRESHOLD
    of it, in which case that one wins instead. The runner-up (by cost)
    for the chosen category is kept as the fallback.

    Returns (quotes_by_category, supplier_log) — quotes_by_category is
    {category: {routed: {...}, fallback: {...} or None, all: [...]}}."""
    passengers = int(passengers)  # defensive: the HTTP route already validates this, but this function should be robust to any caller
    enabled = [s for s in CAB_SUPPLIERS_ENABLED if s in CAB_SUPPLIERS]
    supplier_log = []
    all_normalized = []

    executor = ThreadPoolExecutor(max_workers=max(1, len(enabled)))
    future_to_supplier = {
        executor.submit(CAB_SUPPLIERS[name], pickup, drop, pickup_dt, passengers, luggage): name
        for name in enabled
    }
    try:
        for future in as_completed(future_to_supplier, timeout=CAB_SUPPLIER_TIMEOUT_SECONDS):
            name = future_to_supplier[future]
            try:
                raw_quotes = future.result(timeout=0)
                normalized = [q for q in (_normalize_quote(r) for r in raw_quotes) if q is not None]
                supplier_log.append({
                    "supplier": name, "responded": True, "error": None,
                    "quoteCount": len(normalized),
                    "lowestLandedCost": min((q["totalLandedCost"] for q in normalized), default=None),
                })
                all_normalized.extend(normalized)
            except Exception as e:
                supplier_log.append({"supplier": name, "responded": False, "error": str(e), "quoteCount": 0, "lowestLandedCost": None})
    except TimeoutError:
        # Same reasoning as flight_providers.py: as_completed raises the
        # moment the timeout window closes with futures still running —
        # doesn't silently return just the finished ones.
        pass
    finally:
        # wait=False, deliberately — same fix already proven necessary
        # for flights: a plain context-managed shutdown blocks until
        # every thread finishes, silently defeating the timeout.
        executor.shutdown(wait=False)

    responded_names = {p["supplier"] for p in supplier_log}
    for name in enabled:
        if name not in responded_names:
            supplier_log.append({"supplier": name, "responded": False, "error": "timeout", "quoteCount": 0, "lowestLandedCost": None})

    quotes_by_category = {}
    winners_by_supplier = {}  # supplier -> won_on_reliability (True/False) for the FIRST category they won
    for cat in CATEGORY_ORDER:
        # CAPACITY FLOOR ENFORCEMENT applies here too, not just when a
        # customer explicitly requests a category — a category that
        # can't physically fit the group must never be offered as an
        # option at all, silently or otherwise. Without this check, a
        # 5-passenger search would still surface a SEDAN quote (which
        # only fits 3), directly violating the spec's own explicit rule.
        if passengers > VEHICLE_CATEGORIES[cat]["max_pax"]:
            continue
        cat_quotes = [q for q in all_normalized if q["category"] == cat]
        if not cat_quotes:
            continue
        cat_quotes.sort(key=lambda q: q["totalLandedCost"])
        cheapest = cat_quotes[0]
        routed = cheapest
        won_on_reliability = False
        for candidate in cat_quotes[1:]:
            if candidate["reliabilityScore"] <= routed["reliabilityScore"]:
                continue
            delta = (candidate["totalLandedCost"] - cheapest["totalLandedCost"]) / cheapest["totalLandedCost"]
            if delta <= RELIABILITY_OVERRIDE_THRESHOLD:
                routed = candidate
                won_on_reliability = True
                break  # first higher-reliability supplier within threshold, in ascending price order
        fallback = next((q for q in cat_quotes if q["supplierOfferId"] != routed["supplierOfferId"]), None)
        quotes_by_category[cat] = {"routed": routed, "fallback": fallback, "all": cat_quotes}
        if routed["supplier"] not in winners_by_supplier:
            winners_by_supplier[routed["supplier"]] = won_on_reliability

    for entry in supplier_log:
        entry["wonAnyCategory"] = entry["supplier"] in winners_by_supplier
        entry["wonOnReliability"] = winners_by_supplier.get(entry["supplier"], False)

    if requested_category:
        quotes_by_category = {k: v for k, v in quotes_by_category.items() if k == requested_category}

    # Attach a signed token PER CATEGORY, encoding both the routed
    # supplier AND its pre-computed fallback together — this is what
    # replaces the spec's Redis-cached fallback_provider: the fallback
    # travels with the client in the token itself rather than needing
    # any server-side cache/table at all. Mirrors exactly how flight
    # offers attach their own token in search_flights_aggregated().
    for cat, data in quotes_by_category.items():
        token_payload = {
            "category": cat,
            "routedSupplier": data["routed"]["supplier"],
            "routedOfferId": data["routed"]["supplierOfferId"],
            "routedLandedCost": data["routed"]["totalLandedCost"],
        }
        if data["fallback"]:
            token_payload["fallbackSupplier"] = data["fallback"]["supplier"]
            token_payload["fallbackOfferId"] = data["fallback"]["supplierOfferId"]
            token_payload["fallbackLandedCost"] = data["fallback"]["totalLandedCost"]
        data["cabOfferToken"] = make_signed_token(token_payload)

    return quotes_by_category, supplier_log


def _mock_requote(supplier, supplier_offer_id, original_landed_cost):
    """Simulates re-checking a quote's price right before booking — same
    honesty and same distribution shape as flights' _mock_reprice: most
    of the time unchanged, occasionally up, rarely down, and rarely
    'no longer available' (e.g. that specific car/slot got booked by
    someone else in the meantime)."""
    time.sleep(random.uniform(0.1, 0.4))
    roll = random.random()
    if roll < 0.80:
        return True, original_landed_cost
    elif roll < 0.95:
        return True, round(original_landed_cost + random.uniform(50, 300), 2)
    elif roll < 0.99:
        return True, round(max(300, original_landed_cost - random.uniform(30, 150)), 2)
    else:
        return False, None


def requote_cab(token):
    """THE GUARDRAIL for cabs, mirroring flights' reprice_offer(): called
    right before payment. Verifies the token, re-checks the ROUTED
    supplier's price. If that supplier's price is unchanged, safe to
    book. If it changed, the caller must get explicit re-confirmation.
    If the routed supplier's quote is no longer available at all, this
    is where the pre-computed FALLBACK actually gets used — re-quoted
    the same way, and returned as a substitute rather than failing the
    whole booking outright, since a working fallback was the entire
    point of computing one at search time."""
    valid, payload_or_error = verify_signed_token(token)
    if not valid:
        return {"ok": False, "error": payload_or_error}
    payload = payload_or_error

    available, new_cost = _mock_requote(payload["routedSupplier"], payload["routedOfferId"], payload["routedLandedCost"])
    if available:
        if new_cost == payload["routedLandedCost"]:
            return {"ok": True, "unchanged": True, "usedFallback": False, "supplier": payload["routedSupplier"], "totalLandedCost": new_cost}
        new_token = make_signed_token({k: v for k, v in payload.items() if k != "issuedAt"})
        return {
            "ok": True, "unchanged": False, "usedFallback": False,
            "supplier": payload["routedSupplier"], "oldLandedCost": payload["routedLandedCost"],
            "newLandedCost": new_cost, "newToken": new_token,
        }

    # Routed supplier's quote fell through — fall back to the
    # pre-computed fallback instead of failing outright, exactly the
    # spec's "instant failover" intent, just sourced from the signed
    # token instead of a Redis lookup.
    if "fallbackSupplier" not in payload:
        return {"ok": False, "error": "This quote is no longer available, and no fallback was found. Please search again."}

    fb_available, fb_cost = _mock_requote(payload["fallbackSupplier"], payload["fallbackOfferId"], payload["fallbackLandedCost"])
    if not fb_available:
        return {"ok": False, "error": "Both the original and fallback cab suppliers are no longer available. Please search again."}

    return {
        "ok": True, "unchanged": False, "usedFallback": True,
        "supplier": payload["fallbackSupplier"], "totalLandedCost": fb_cost,
        "newToken": make_signed_token({
            "category": payload["category"], "routedSupplier": payload["fallbackSupplier"],
            "routedOfferId": payload["fallbackOfferId"], "routedLandedCost": fb_cost,
        }),
    }
