# Tripbookerz OTP Backend (MSG91)

A minimal Flask server that wraps MSG91's v5 OTP API, so your MSG91 auth key
never has to touch the frontend — plus a persistent Postgres-backed data
store for bookings, coupons, campaigns, and platform markup config,
replacing the browser's `localStorage` with something real, shared, and
durable.

## 1. Prerequisites (MSG91 side)

1. Sign up / log in at https://control.msg91.com
2. Get your **Auth Key** (Account settings).
3. Create an **OTP Template** (Dashboard → OTP → Templates) with `{{otp}}`
   in the message body. Note the resulting **Template ID**.
4. Complete **DLT registration** (required by Indian telecom regulation for
   any transactional/OTP SMS). Without this, sends will fail or silently
   not deliver to Indian numbers, even once your code is correct.

## 2. Prerequisites (database — Neon)

1. Sign up at https://neon.tech (permanent free tier: 0.5 GB storage, no
   time-limited trial — it suspends when idle and wakes on the next
   request, rather than expiring like some competitors' free databases).
2. Create a project.
3. Go to **Connection Details** and copy the connection string — it looks
   like:
   ```
   postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require
   ```
4. That's your `DATABASE_URL`. Tables are created automatically on first
   run (`models.py` calls `Base.metadata.create_all()`), no manual SQL
   migration needed for this stage.

## 3. Local setup

```bash
cd tripbookerz-otp-backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in MSG91_AUTH_KEY, MSG91_TEMPLATE_ID, and DATABASE_URL
```

Load the .env file before running (or use a tool like `python-dotenv` /
your platform's environment variable settings). Quick manual way:

```bash
export $(cat .env | xargs)   # macOS/Linux
python app.py
```

Server starts at `http://localhost:5000`. Check `/health` — it now also
reports whether the database connected: `{"status":"ok","database":true}`.

## 4. Endpoints

### OTP

| Method | Path               | Body                                   |
|--------|--------------------|-----------------------------------------|
| POST   | /api/send-otp      | `{ "mobile": "9545555638" }`            |
| POST   | /api/verify-otp    | `{ "mobile": "9545555638", "otp": "123456" }` |
| POST   | /api/resend-otp    | `{ "mobile": "9545555638", "retrytype": "text" }` |

### Bookings

| Method | Path                     | Notes |
|--------|--------------------------|-------|
| POST   | /api/bookings            | Create a booking. Body matches the frontend's booking record shape (`bookingId`, `pnr`, `kind`, `title`, `net`, `platformFee`, `agentFee`, `discount`, `total`, `role`, `traveler`, `details`). |
| GET    | /api/bookings            | List all, newest first. `?role=agent` filters. |
| GET    | /api/bookings/&lt;id&gt; | Look up by Tripbookerz Booking ID **or** the airline/hotel partner reference — same dual lookup the admin panel already does. |

### Coupons / Campaigns / Markup config

| Method | Path                        | Notes |
|--------|-----------------------------|-------|
| POST   | /api/coupons                | Create. |
| GET    | /api/coupons                | List all. |
| DELETE | /api/coupons/&lt;code&gt;   | Delete. |
| POST   | /api/coupons/&lt;code&gt;/use | Validates (date window + usage cap) and atomically increments usage — call this when a booking is *confirmed*, not just when a code is typed in. |
| POST   | /api/campaigns              | Create. |
| GET    | /api/campaigns              | List all. |
| PATCH  | /api/campaigns/&lt;id&gt;   | Toggle active/paused. |
| GET    | /api/markup-config          | Get current platform markup. |
| PUT    | /api/markup-config          | Set platform markup. |

All of the above return `500` with a clear error if `DATABASE_URL` isn't
configured, rather than crashing.

## 5. Wiring into the Tripbookerz frontend

This is the next step, not yet done: the portal's `state` object
(`localStorage`-backed) needs its booking/coupon/campaign/markup functions
switched from reading/writing `localStorage` to calling these endpoints
with `fetch()` instead — same pattern as the OTP wiring described below.
Ask for this explicitly when ready; it touches `finalizeBooking()`,
`addCoupon()`, `deleteCoupon()`, `addCampaign()`, `toggleCampaign()`,
`saveMarkup()`, and the admin booking-lookup function in `index.html`.

### OTP wiring (already documented, unchanged)

Replace the current client-side `sendOTP()` / `verifyOTP()` functions
(which fake the OTP in the browser) with real calls to this backend:

```javascript
async function sendOTP(){
  const phone = document.getElementById('tv-phone').value.trim();
  const res = await fetch('https://your-backend-domain.com/api/send-otp', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ mobile: phone })
  });
  const data = await res.json();
  if(data.success){
    document.getElementById('otp-row').style.display = 'block';
  } else {
    // show error to user
  }
}

async function verifyOTP(){
  const phone = document.getElementById('tv-phone').value.trim();
  const otp = document.getElementById('tv-otp').value.trim();
  const res = await fetch('https://your-backend-domain.com/api/verify-otp', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ mobile: phone, otp })
  });
  const data = await res.json();
  travelerInfo.phoneVerified = data.verified;
  // update UI: show green "verified" or red "incorrect OTP" based on data.verified
}
```

No OTP value is ever generated or checked in the browser anymore — MSG91
holds and verifies it server-side, and this backend is just a secure
relay.

## 6. Deploying

Any standard Python host works: Render, Railway, a small DigitalOcean/AWS
VM behind Gunicorn + nginx, etc. Set `MSG91_AUTH_KEY`, `MSG91_TEMPLATE_ID`,
and `DATABASE_URL` as environment variables in that platform's dashboard
— don't bake them into the deployed code or a committed `.env` file.

Restrict CORS (`CORS(app, origins=[...])` in `app.py`) to your real
frontend domain before going live; `CORS(app)` (allow-all) is fine for
local testing only.

## 7. Security guardrails — what's actually in place vs. not

**Fixed in this version:**
- **Admin authentication.** Endpoints that expose traveler PII or control pricing/promotions (booking lookup/list, coupon create/list/delete, campaign create/toggle, changing markup config) now require an `X-Admin-Key` header matching `ADMIN_API_KEY`. This is a **shared-secret stopgap for pre-prod**, not real multi-user auth — it can't tell one admin/agent apart from another, and the key must be rotated manually if it ever leaks. Replace with real per-user accounts + roles before this handles real staff with different permission levels.
- **Server-side fare trust boundary.** `POST /api/bookings` no longer accepts `platformFee`, `discount`, or `total` from the client as truth — it recomputes `platformFee` from the server's own `MarkupConfig` row, recomputes `discount` from the actual `Coupon` row (if a `couponCode` is passed), and always recalculates `total` itself. A modified frontend can no longer just declare its own price.
- **Coupon redemption race condition.** Both `/api/coupons/<code>/use` and the coupon-aware path inside `/api/bookings` now use a single atomic conditional `UPDATE ... WHERE used < max_uses` instead of read-then-write, so concurrent requests can't push a coupon past its usage cap.

**Still open — not fixed, listed so it isn't lost:**
- **`net` (the airline/hotel fare) is still accepted from the client.** This backend has no live GDS/hotel feed of its own to verify it against yet — that's a larger, separate integration, not a quick fix here.
- **No audit history.** Changing `MarkupConfig` overwrites the single row with no record of the previous value, who changed it, or when. Same for coupon/campaign edits. If "who changed the markup and when" needs to be answerable later, this needs an append-only audit log table, not yet built.
- **agentFee is still client-supplied**, since it's a legitimate per-booking discretionary value an agent sets — but nothing here bounds it (e.g. a max % cap), so an agent-facing client could currently submit an unreasonable fee.

**CORS — now environment-conditional, not a manual-edit comment:**
Previously this was `CORS(app)` (allow-all) with a comment saying "restrict this before going live" — exactly the kind of thing that's easy to forget. It's now driven by the same `DEPLOY_ENV` variable as the rate-limiter gate: staging defaults to allow-all (convenient for testing from anywhere), but `DEPLOY_ENV=production` makes `CORS_ORIGINS` mandatory and **refuses to start** without it. Set `CORS_ORIGINS` in staging too, any time you want to test the restricted behavior early.

**Rate limiting — added, with a known limitation to plan around:**
`/api/send-otp` (5/10min per IP), `/api/verify-otp` (10/10min), `/api/resend-otp` (3/10min), and `POST /api/bookings` (10/10min) are now rate-limited per IP address. This closes the real financial risk of someone hammering the OTP-send endpoint and running up your MSG91 SMS bill, or flooding the database with fake bookings — both of which are public-by-necessity endpoints (real customers need to call them without logging in), so an admin key can't protect them the way it protects the dashboard endpoints.

**The limitation to know about:** this is a simple in-memory counter, not Flask-Limiter + Redis. It works correctly on a single Render instance, but resets on every restart/redeploy, and won't share limits correctly if you ever scale to multiple instances. Fine for pre-prod; revisit before scaling past one instance.

None of this is production-ready for real payments as-is. It's meaningfully safer than the version before this pass, and honest about exactly where the remaining risk sits.

## 8. What's been tested vs. not

The OTP routes (send/verify/resend) were exercised end-to-end with Flask's
test client and the real MSG91 call mocked out — full route/validation
logic confirmed, only the actual live MSG91 network call is unverified
until you run it with real credentials.

The database layer (`models.py` + the booking/coupon/campaign/markup
routes) could **not** be executed in the environment this was built in —
no network access to install SQLAlchemy or reach a live Postgres instance.
What *is* confirmed: both files compile cleanly (valid Python syntax) and
follow correct SQLAlchemy 2.0 API patterns. The first real test of this
code is the first time you run it against your actual Neon database —
budget a little time for that before assuming it's production-ready.

