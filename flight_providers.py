"""
Multi-provider flight fare aggregation.

WHY THIS IS ITS OWN FILE, not more code in app.py: this is meant to be
"plug and play" — adding a real provider later, or removing a mocked
one, should mean editing THIS file only. app.py just imports
search_flights_aggregated() and reprice_offer() and calls them from two
routes; it never talks to a specific provider directly.

HONEST STATE TODAY: all six providers below are MOCKS. None of them
call a real API — there are no real accounts/API keys for any of them
yet. They exist so the actual aggregation architecture (parallel
querying, timeouts, normalization, deduplication, offer-token signing,
re-pricing) is real and tested NOW, and swapping in a real provider
later is "replace one function body," not a redesign.

The six providers are the ones actually named in the integration guide
this was built from — not invented:
  - Duffel            (NDC fares, full-service carriers)
  - Mystifly          (global sourcing, POS currency arbitrage)
  - Travelfusion      (budget/LCC direct scraping + NDC)
  - TBO Holidays      (Indian domestic LCC coverage — the doc's own
                       pick for India, since global GDS/aggregators
                       often miss domestic LCC inventory)
  - Amadeus Self-Service (search-focused GDS aggregator)
  - Kiwi (Tequila API)   (virtual interlining / budget)

TO ADD A REAL PROVIDER LATER:
  1. Write one function matching search_flights(from_code, to_code,
     date) -> list[dict], returning offers in the NORMALIZED_OFFER_KEYS
     shape below (see any existing _mock_* function for the exact shape).
  2. Write its matching reprice(provider_offer_id) -> (ok, new_price) function.
  3. Add one line each to FLIGHT_PROVIDERS and FLIGHT_REPRICERS below.
  4. Add its name to FLIGHT_PROVIDERS_ENABLED (env var) and set its
     real credentials as their own env vars.
Nothing else changes — app.py, the routes, the token signing, the
dedup logic are all provider-agnostic.
"""

import os
import time
import random
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from offer_tokens import make_signed_token, verify_signed_token

# ---------- Configuration ----------
# Comma-separated list of which mocked providers are actually queried.
# Defaults to ALL of them — "keep as many as possible for now, evaluate
# and trim later" was the explicit instruction, so nothing is disabled
# by default. Trim this list (or extend it with a real provider's name)
# without touching any other code.
FLIGHT_PROVIDERS_ENABLED = [
    p.strip() for p in os.environ.get(
        "FLIGHT_PROVIDERS_ENABLED",
        "duffel,mystifly,travelfusion,tbo,amadeus,kiwi"
    ).split(",") if p.strip()
]

# How long the aggregator waits for ALL providers before giving up on
# the slow ones and returning whatever DID respond in time. The
# integration guide specifically calls for a 3-5 second window.
FLIGHT_PROVIDER_TIMEOUT_SECONDS = float(os.environ.get("FLIGHT_PROVIDER_TIMEOUT_SECONDS", "4.0"))

AIRLINES_FULL_SERVICE = ["Air India", "Vistara"]
AIRLINES_LCC = ["IndiGo", "SpiceJet", "Akasa Air"]


def _normalized_offer(provider, provider_offer_id, airline, flight_no, from_code, to_code,
                       dep_time, arr_time, date, duration_hrs, fare_net, currency="INR"):
    """The one shape every provider adapter must return each offer in.
    Keeping this in one function (rather than each adapter building its
    own dict literal) means changing the shape later is a one-place edit."""
    return {
        "provider": provider,
        "providerOfferId": provider_offer_id,
        "airline": airline,
        "flightNo": flight_no,
        "fromCode": from_code,
        "toCode": to_code,
        "depTime": dep_time,
        "arrTime": arr_time,
        "date": date,
        "durationHrs": duration_hrs,
        "fareNet": fare_net,
        "currency": currency,
    }


def _mock_latency(min_ms, max_ms):
    """Simulates real network round-trip time. A wide, provider-specific
    range is intentional — it's what makes the timeout-handling and
    "some providers are just slower" behavior actually testable, not
    just theoretical."""
    time.sleep(random.uniform(min_ms, max_ms) / 1000.0)


def _mock_flight_basics(from_code, to_code, date, seed):
    rnd = random.Random(seed)
    dep_hour = rnd.randint(5, 21)
    dep_min = rnd.choice([0, 15, 30, 45])
    dur_hrs = round(rnd.uniform(1.2, 3.2), 1)
    arr_total_min = dep_hour * 60 + dep_min + round(dur_hrs * 60)
    arr_hour = (arr_total_min // 60) % 24
    arr_min = arr_total_min % 60
    dep_time = f"{dep_hour:02d}:{dep_min:02d}"
    arr_time = f"{arr_hour:02d}:{arr_min:02d}"
    flight_no = str(rnd.randint(100, 999))
    return dep_time, arr_time, dur_hrs, flight_no


# ---------- Provider adapters (all mocked — see module docstring) ----------

def _duffel_search(from_code, to_code, date):
    # Duffel: NDC fares, full-service carriers — priced accordingly
    # (higher than LCC-focused providers), per the doc's own description.
    _mock_latency(300, 900)
    offers = []
    for i, airline in enumerate(AIRLINES_FULL_SERVICE):
        dep, arr, dur, no = _mock_flight_basics(from_code, to_code, date, hash((from_code, to_code, date, "duffel", i)))
        fare = random.Random(hash((from_code, to_code, date, "duffel", i, "fare"))).randint(5200, 8200)
        offers.append(_normalized_offer("duffel", f"duf_{secrets.token_hex(6)}", airline, no,
                                         from_code, to_code, dep, arr, date, dur, fare))
    return offers


def _mystifly_search(from_code, to_code, date):
    # Mystifly: global sourcing + POS currency arbitrage — a mid-range
    # mix across both full-service and LCC inventory.
    _mock_latency(400, 1200)
    offers = []
    for i, airline in enumerate(AIRLINES_FULL_SERVICE + AIRLINES_LCC[:1]):
        dep, arr, dur, no = _mock_flight_basics(from_code, to_code, date, hash((from_code, to_code, date, "mystifly", i)))
        fare = random.Random(hash((from_code, to_code, date, "mystifly", i, "fare"))).randint(4800, 7500)
        offers.append(_normalized_offer("mystifly", f"myst_{secrets.token_hex(6)}", airline, no,
                                         from_code, to_code, dep, arr, date, dur, fare))
    return offers


def _travelfusion_search(from_code, to_code, date):
    # Travelfusion: "world leader for budget/LCC via direct scraping and
    # NDC feeds" per the doc — priced lowest among the six, reflecting
    # that positioning.
    _mock_latency(500, 1500)
    offers = []
    for i, airline in enumerate(AIRLINES_LCC):
        dep, arr, dur, no = _mock_flight_basics(from_code, to_code, date, hash((from_code, to_code, date, "travelfusion", i)))
        fare = random.Random(hash((from_code, to_code, date, "travelfusion", i, "fare"))).randint(3600, 5800)
        offers.append(_normalized_offer("travelfusion", f"tf_{secrets.token_hex(6)}", airline, no,
                                         from_code, to_code, dep, arr, date, dur, fare))
    return offers


def _tbo_search(from_code, to_code, date):
    # TBO Holidays: the doc's own explicit pick for Indian domestic LCC
    # coverage/net rates — priced competitively for domestic routes,
    # reflecting the doc's point that GDS/global aggregators often miss
    # this inventory entirely.
    _mock_latency(250, 800)
    offers = []
    for i, airline in enumerate(AIRLINES_LCC):
        dep, arr, dur, no = _mock_flight_basics(from_code, to_code, date, hash((from_code, to_code, date, "tbo", i)))
        fare = random.Random(hash((from_code, to_code, date, "tbo", i, "fare"))).randint(3400, 5600)
        offers.append(_normalized_offer("tbo", f"tbo_{secrets.token_hex(6)}", airline, no,
                                         from_code, to_code, dep, arr, date, dur, fare))
    return offers


def _amadeus_search(from_code, to_code, date):
    # Amadeus Self-Service: search-focused GDS aggregator — broad
    # coverage across both segments, mid-to-upper pricing.
    _mock_latency(350, 1000)
    offers = []
    for i, airline in enumerate(AIRLINES_FULL_SERVICE + AIRLINES_LCC[1:2]):
        dep, arr, dur, no = _mock_flight_basics(from_code, to_code, date, hash((from_code, to_code, date, "amadeus", i)))
        fare = random.Random(hash((from_code, to_code, date, "amadeus", i, "fare"))).randint(5000, 7800)
        offers.append(_normalized_offer("amadeus", f"ama_{secrets.token_hex(6)}", airline, no,
                                         from_code, to_code, dep, arr, date, dur, fare))
    return offers


def _kiwi_search(from_code, to_code, date):
    # Kiwi (Tequila API): virtual interlining / budget-focused.
    _mock_latency(400, 1100)
    offers = []
    for i, airline in enumerate(AIRLINES_LCC[:2]):
        dep, arr, dur, no = _mock_flight_basics(from_code, to_code, date, hash((from_code, to_code, date, "kiwi", i)))
        fare = random.Random(hash((from_code, to_code, date, "kiwi", i, "fare"))).randint(3800, 6000)
        offers.append(_normalized_offer("kiwi", f"kiwi_{secrets.token_hex(6)}", airline, no,
                                         from_code, to_code, dep, arr, date, dur, fare))
    return offers


# Registry — the ONE place a new provider gets added.
FLIGHT_PROVIDERS = {
    "duffel": _duffel_search,
    "mystifly": _mystifly_search,
    "travelfusion": _travelfusion_search,
    "tbo": _tbo_search,
    "amadeus": _amadeus_search,
    "kiwi": _kiwi_search,
}

# Which registered providers are mocks vs real — all six are mocks
# today (see the module docstring). When a real integration replaces
# one of these, flip its entry to False IN THE SAME EDIT that swaps in
# the real function — this is what app.py's
# enforce_no_mock_providers_in_production() checks at startup, so a
# real deployment can never silently serve fake fares just because
# someone forgot a separate step.
FLIGHT_PROVIDERS_IS_MOCK = {
    "duffel": True,
    "mystifly": True,
    "travelfusion": True,
    "tbo": True,
    "amadeus": True,
    "kiwi": True,
}


def _mock_reprice(provider, provider_offer_id, original_fare):
    """Simulates calling a provider's real re-price/recheck endpoint.
    Most of the time the price is unchanged (inventory didn't move);
    occasionally it's gone up (sold out at that fare bucket) or, more
    rarely, down. This distribution is arbitrary but deliberately
    weighted toward 'unchanged', matching real-world fare-bucket
    behavior — a price recheck a few minutes after search usually comes
    back the same."""
    _mock_latency(200, 700)
    roll = random.random()
    if roll < 0.75:
        return True, original_fare
    elif roll < 0.93:
        bump = random.randint(100, 800)
        return True, original_fare + bump
    elif roll < 0.98:
        drop = random.randint(50, 400)
        return True, max(500, original_fare - drop)
    else:
        # Sold out entirely at any price in this fare bucket.
        return False, None


FLIGHT_REPRICERS = {
    name: _mock_reprice for name in FLIGHT_PROVIDERS
}


# ---------- Aggregation: parallel query, normalize, dedupe, sign ----------

def search_flights_aggregated(from_code, to_code, date):
    """Queries every enabled provider in parallel, waits up to
    FLIGHT_PROVIDER_TIMEOUT_SECONDS, deduplicates by
    [airline + flightNo + depTime] keeping the cheapest offer for each,
    and returns (results, provider_log) where provider_log records what
    actually happened with each provider for this search (for the
    analytics table — see models.FlightProviderLog)."""
    enabled = [p for p in FLIGHT_PROVIDERS_ENABLED if p in FLIGHT_PROVIDERS]
    provider_log = []
    all_offers = []

    executor = ThreadPoolExecutor(max_workers=max(1, len(enabled)))
    future_to_provider = {
        executor.submit(FLIGHT_PROVIDERS[name], from_code, to_code, date): name
        for name in enabled
    }
    try:
        for future in as_completed(future_to_provider, timeout=FLIGHT_PROVIDER_TIMEOUT_SECONDS):
            name = future_to_provider[future]
            try:
                offers = future.result(timeout=0)
                provider_log.append({
                    "provider": name, "responded": True, "error": None,
                    "offerCount": len(offers),
                    "lowestFare": min((o["fareNet"] for o in offers), default=None),
                })
                all_offers.extend(offers)
            except Exception as e:
                provider_log.append({
                    "provider": name, "responded": False, "error": str(e),
                    "offerCount": 0, "lowestFare": None,
                })
    except TimeoutError:
        # Expected, not a bug: as_completed() raises this the moment the
        # timeout window closes with one or more futures still running.
        # Whichever providers already got appended to provider_log above
        # are real, on-time responses; anything not yet in provider_log
        # gets logged as a timeout below.
        pass
    finally:
        # wait=False is the actual fix here — NOT the default. A plain
        # `with ThreadPoolExecutor(...) as executor:` block's __exit__
        # calls shutdown(wait=True) implicitly, which blocks THIS
        # function until every submitted thread finishes — including
        # ones already excluded above by the timeout. That silently
        # defeated the entire point of having a timeout: the search
        # would still take as long as the slowest provider, every time.
        # wait=False lets this function return promptly; the slow
        # thread keeps running harmlessly in the background and its
        # result is simply never read by anything.
        executor.shutdown(wait=False)

    # Providers that never even completed within the timeout window
    # (as_completed simply won't yield their future) are still logged
    # as non-responses, not silently dropped.
    responded_names = {p["provider"] for p in provider_log}
    for name in enabled:
        if name not in responded_names:
            provider_log.append({
                "provider": name, "responded": False, "error": "timeout",
                "offerCount": 0, "lowestFare": None,
            })

    # Deduplicate: group by [airline + flightNo + depTime], keep cheapest.
    best_by_key = {}
    for offer in all_offers:
        key = (offer["airline"], offer["flightNo"], offer["depTime"])
        if key not in best_by_key or offer["fareNet"] < best_by_key[key]["fareNet"]:
            best_by_key[key] = offer

    # Mark which providers actually "won" at least one deduplicated
    # result — this is what makes provider evaluation later meaningful
    # (not just "did it respond" but "did it actually offer the winning
    # price for anything").
    winning_providers = {o["provider"] for o in best_by_key.values()}
    for entry in provider_log:
        entry["wonAnyOffer"] = entry["provider"] in winning_providers

    results = []
    for offer in sorted(best_by_key.values(), key=lambda o: o["fareNet"]):
        token = make_offer_token(offer["provider"], offer["providerOfferId"], offer["fareNet"])
        result = dict(offer)
        result["offerToken"] = token
        results.append(result)

    return results, provider_log


# ---------- Offer tokens: HMAC-signed, so a client can never alter which
# provider/offer/price a booking actually routes to or charges ----------
# Thin, flight-specific wrappers around the shared offer_tokens module
# (see that file) — the actual signing/verification logic lives there
# now, shared with cab_providers.py, not duplicated here.

def make_offer_token(provider, provider_offer_id, fare_net):
    return make_signed_token({
        "provider": provider,
        "providerOfferId": provider_offer_id,
        "fareNet": fare_net,
    })


def verify_offer_token(token):
    return verify_signed_token(token)


def reprice_offer(token):
    """THE GUARDRAIL: called right before payment. Verifies the token,
    re-checks the price with the SAME provider the offer actually came
    from, and returns a clear result the caller must act on:
      {ok: True, unchanged: True}                    -> safe to charge
      {ok: True, unchanged: False, newFare, newToken} -> price moved,
          caller MUST get explicit re-confirmation before charging,
          and must use newToken (not the old one) if they proceed
      {ok: False, error}                              -> token invalid/
          expired, or the provider says this offer is no longer
          available at any price (sold out)
    Never silently charges a different price than what was last shown
    and accepted — that's the entire point of this function existing.
    """
    valid, payload_or_error = verify_offer_token(token)
    if not valid:
        return {"ok": False, "error": payload_or_error}

    payload = payload_or_error
    provider = payload["provider"]
    repricer = FLIGHT_REPRICERS.get(provider)
    if not repricer:
        return {"ok": False, "error": f"Unknown provider '{provider}' — cannot re-price"}

    available, new_fare = repricer(provider, payload["providerOfferId"], payload["fareNet"])
    if not available:
        return {"ok": False, "error": "This fare is no longer available. Please search again."}

    if new_fare == payload["fareNet"]:
        return {"ok": True, "unchanged": True, "fareNet": new_fare}

    new_token = make_offer_token(provider, payload["providerOfferId"], new_fare)
    return {
        "ok": True, "unchanged": False,
        "oldFare": payload["fareNet"], "newFare": new_fare,
        "newToken": new_token,
    }
