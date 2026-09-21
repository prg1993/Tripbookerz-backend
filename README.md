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

### Agents (registration, verification, approval, login)

Real authentication — replaces the frontend's old "click Agent to become
an agent" button. Flow: **register → verify phone (real, MSG91) → verify
email (see honest caveat below) → admin approves → agent can log in.**

| Method | Path                              | Auth | Notes |
|--------|-----------------------------------|------|-------|
| POST   | /api/agents/register              | none | Body: `entityType` (individual/company), `name`, `registeredAddress`, `phone`, `email`, `password` (min 8 chars), `gstNo` (required if company; format-validated only, not checked against GSTN), `panNo` (optional). Triggers a real MSG91 OTP send. Rate-limited: 5/hour/IP. |
| POST   | /api/agents/verify-phone          | none | `{ "agentId", "otp" }` — verifies against MSG91 for real. |
| POST   | /api/agents/verify-email          | none | `{ "agentId", "code" }` — see email caveat below. |
| POST   | /api/agents/resend-email-code     | none | `{ "agentId" }` |
| POST   | /api/agents/login                 | none | `{ "email", "password" }` → returns a bearer `token` (only once `status` is `approved`). |
| GET    | /api/agents/me                    | `Agent-Token` header | Confirms a session is valid, returns the agent's own record. |
| GET    | /api/agents                       | `X-Admin-Key` | List all agents. `?status=pending_approval` filters. |
| POST   | /api/agents/&lt;id&gt;/approve    | `X-Admin-Key` | Only works from `pending_approval` (i.e. both phone and email already verified). |
| POST   | /api/agents/&lt;id&gt;/reject     | `X-Admin-Key` | |

**Honest gap: email is not actually sent yet.** There's no email provider
connected (SendGrid/SES/etc. — same gap flagged for booking confirmations
earlier). In staging (`DEPLOY_ENV` != `production`), the verification code
is returned directly in the `/register` and `/resend-email-code` response
bodies (`dev_email_code`) so the full flow can still be tested end-to-end.
**This must be replaced with a real email send before production** — right
now a production deploy would generate a code that never reaches the
agent, silently blocking every registration at the email-verification
step. Worth wiring up before this matters for real users.

**Session tokens** are a single opaque bearer token per agent (logging in
again replaces the old one — no multi-device session list). 12-hour
expiry. Sent as the `Agent-Token` header, not a cookie — deliberately
avoids cross-origin cookie/SameSite complications since the frontend
(Hostinger) and backend (Render) are different domains.

### Admin employee accounts, login, audit log

Replaces the single shared `X-Admin-Key` as something typed into the
website. The master key still exists, but only as a **bootstrapping
credential used outside the public site** (curl/Postman) to create named
employee accounts — from then on, employees log in with their own
email/password, and every admin action is attributed to a real person.

| Method | Path                                 | Auth | Notes |
|--------|---------------------------------------|------|-------|
| POST   | /api/admin/users                      | Master key only | Create an employee account: `{ "name", "email", "password" }`. **Not reachable with an employee token** — see `require_master_key` — so no employee can mint more employee accounts. |
| GET    | /api/admin/users                      | Master key only | List all employee accounts. |
| POST   | /api/admin/users/&lt;id&gt;/disable   | Master key only | Immediately invalidates that employee's active session too. |
| POST   | /api/admin/login                      | none | `{ "email", "password" }` → bearer token (`Admin-Token` header), 12h expiry. Rate-limited: 10/10min/IP. |
| GET    | /api/admin/me                         | master key OR `Admin-Token` | Confirms a session/key is valid, returns the resolved actor. |
| GET    | /api/admin/audit-log                  | master key OR `Admin-Token` | `?action=coupon.create`, `?actorId=TBZADM...`, `?limit=100` (max 500) to filter/page. |

**Every existing admin-gated endpoint** (coupons, campaigns, markup
config, agent approve/reject) now accepts **either** the master key or a
real employee token — same protection level, but now attributes the
action to whoever actually did it.

**What gets logged, concretely:** `coupon.create`, `coupon.delete`,
`campaign.create`, `campaign.toggle`, `markup_config.update` (captures
before/after values), `agent.approve`, `agent.reject`,
`admin_user.create`, `admin_user.disable`, `admin_user.login` — each
entry has the actor's name/id, the action, the target, contextual
details, IP address, and timestamp. The audit table is append-only —
nothing in the application code updates or deletes a row once written.

**Creating your first employee account** (there's no UI for this
deliberately — see the "not visible to customers" note below):
```bash
curl -X POST https://your-backend.onrender.com/api/admin/users \
  -H "X-Admin-Key: your-master-key" \
  -H "Content-Type: application/json" \
  -d '{"name":"Priya Sharma","email":"priya@tripbookerz.com","password":"a-real-password-here"}'
```


### Confirmation emails (Resend)

**This replaces the previous fully-simulated email.** Booking confirmations
are now sent for real, with the e-ticket/voucher PDF attached.

Uses [Resend](https://resend.com), chosen specifically because its free
tier is **permanent** (3,000 emails/month) — SendGrid's free tier was
retired in 2025 and new signups now only get a 60-day trial before
requiring payment, confirmed at the time this was built.

1. Sign up at resend.com, get an **API key** (Dashboard → API Keys).
2. To send from `no-reply@tripbookerz.com` (or any address on your real
   domain), you must **verify that domain** in Resend (Dashboard →
   Domains → Add Domain → add the DNS records they give you). **Until
   that's done, Resend only delivers from its own sandbox address**
   (`onboarding@resend.dev`) — sending from an unverified domain's
   address fails outright, it doesn't silently work with reduced
   deliverability.
3. Set `RESEND_API_KEY` and `EMAIL_FROM` as described in `.env.example`.

| Method | Path                          | Auth | Notes |
|--------|-------------------------------|------|-------|
| POST   | /api/send-confirmation-email  | none, rate-limited | `{ "to", "subject", "html", "pdfFilename"?, "pdfBase64"? }`. Public and rate-limited (10/10min/IP) for the same reason send-otp is — a real customer triggers this right after booking, without being logged in. Cc's support@tripbookerz.com automatically. |

### Bookings

| Method | Path                              | Notes |
|--------|------------------------------------|-------|
| POST   | /api/bookings                     | Create a booking. Body matches the frontend's booking record shape (`bookingId`, `pnr`, `kind`, `title`, `net`, `platformFee`, `agentFee`, `discount`, `total`, `role`, `traveler`, `details`). |
| GET    | /api/bookings                     | List all, newest first. `?role=agent` filters. |
| GET    | /api/bookings/&lt;id&gt;          | Admin-only. Look up by Tripbookerz Booking ID **or** the airline/hotel partner reference. |
| POST   | /api/bookings/lookup              | **Public**, rate-limited. `{ "bookingId", "phone" }` — both must match before anything is returned. Used by the support chat's "check my booking" flow. A booking ID alone is never sufficient (it's not secret) — see the endpoint's docstring. |
| PATCH  | /api/bookings/&lt;id&gt;/refund-status | Admin-only. `{ "status": "none"\|"requested"\|"processing"\|"refunded"\|"rejected", "amount"?, "notes"? }` — this is the ONLY way refund status ever changes; nothing client-facing sets it. Audit-logged with before/after. |

### Callback requests (holiday package lead capture)

The support chat treats holiday package questions as an exception: instead of
answering from the FAQ bot, it asks for a phone number and captures a real
callback request — packages are the high-revenue product, so a human
follow-up converts better than a canned answer.

| Method | Path                                    | Notes |
|--------|------------------------------------------|-------|
| POST   | /api/callback-requests                  | Public, rate-limited (5/10min/IP). `{ "phone", "context"? }` |
| GET    | /api/callback-requests                  | Admin-only. `?status=new` to filter. |
| PATCH  | /api/callback-requests/&lt;id&gt;/status | Admin-only. `{ "status": "new"\|"contacted"\|"closed" }` |

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

**Agent authentication (added most recently):** everything that doesn't
require a live database was actually tested, not just written — password
hashing (confirms a hash isn't the plaintext, correct password verifies,
wrong password is rejected), GST format regex (tested against real GSTIN
numbers, including one from a document you provided earlier, plus
malformed examples), agent ID generation (1000 samples, all unique,
correct format), session token generation (1000 samples, all unique),
and verification-code expiry logic. The database-dependent parts
(register/verify/approve/login actually writing to and reading from
Postgres) have the same untested status as the rest of the DB layer —
first real test is your first live run against Neon.

**Admin employee accounts + audit log:** admin ID generation (1000
samples, unique, correct format) and the two-tier auth resolution logic
(master key match/mismatch, valid/expired employee token, neither
provided) were tested standalone, including explicitly confirming an
employee token can never satisfy `require_master_key` — that's the
property preventing an employee from creating more employee accounts.
Same caveat as everywhere else: the actual database reads/writes
(creating accounts, logging in, writing audit rows) are untested until
run against a live Neon database.

