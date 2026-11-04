"""
Database models for Tripbookerz.

Mirrors the data that currently lives only in the browser's localStorage
(see the `state` object in the portal's index.html) — this is the real,
shared, durable version of that same data.
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean, DateTime, JSON, inspect, text
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Booking(Base):
    __tablename__ = "bookings"

    # The Tripbookerz Booking ID (e.g. TBZF260917A8XQ2) — mandatory, always
    # system-generated, primary key for lookups.
    booking_id = Column(String(32), primary_key=True)

    # The airline/hotel's own reference (6-char PNR or 10-digit hotel ref).
    partner_ref = Column(String(20), nullable=False, index=True)

    kind = Column(String(20), nullable=False)       # flight | hotel | itinerary
    title = Column(String(255), nullable=False)

    net_fare = Column(Integer, nullable=False)
    platform_fee = Column(Integer, nullable=False)
    agent_fee = Column(Integer, nullable=False, default=0)
    discount = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False)

    booked_by_role = Column(String(20), nullable=False)  # customer | agent
    created_at = Column(DateTime, default=datetime.utcnow)

    traveler_name = Column(String(255))
    traveler_phone = Column(String(20))
    traveler_phone_verified = Column(Boolean, default=False)
    traveler_email = Column(String(255))

    # Dedicated, indexed columns — not buried in the `details` JSON blob
    # below — specifically because real audit/finance reconciliation
    # needs to search/filter by payment reference efficiently ("find the
    # booking matching payment ref X" against the gateway's own records),
    # which a JSON field can't do well. See PAYMENT_PROVIDER in app.py
    # for the honest current state: mock only, no real gateway yet — this
    # column holds whatever reference that mock (or, later, a real
    # gateway) actually returned, never a client-supplied guess.
    payment_mode = Column(String(30))
    payment_reference = Column(String(64), index=True)

    # Route/stay/itinerary specifics — kept as JSON since the shape differs
    # by kind (flight vs hotel vs package), same as the frontend's `details`.
    details = Column(JSON)

    # Refund/cancellation tracking. Deliberately a lightweight status
    # field, not the full refund transaction lifecycle (separate Refund
    # table with supplier/payment-gateway references) — that's a bigger,
    # separate piece of work. This exists so "check my refund status" can
    # report something REAL, sourced from what an admin has actually set
    # after processing a cancellation, rather than never being answerable
    # or (worse) being guessed/fabricated by anything client-facing.
    refund_status = Column(String(20), nullable=False, default="none")
    # none | requested | processing | refunded | rejected
    refund_amount = Column(Integer)          # set once known/approved
    refund_requested_at = Column(DateTime)
    refund_updated_at = Column(DateTime)
    refund_notes = Column(String(500))       # short admin-facing note, e.g. reason if rejected

    def to_dict(self):
        return {
            "bookingId": self.booking_id,
            "pnr": self.partner_ref,
            "kind": self.kind,
            "title": self.title,
            "net": self.net_fare,
            "platformFee": self.platform_fee,
            "agentFee": self.agent_fee,
            "discount": self.discount,
            "total": self.total,
            "role": self.booked_by_role,
            "ts": self.created_at.isoformat() + "Z" if self.created_at else None,
            "paymentMode": self.payment_mode,
            "paymentReference": self.payment_reference,
            "refundStatus": self.refund_status,
            "refundAmount": self.refund_amount,
            "refundRequestedAt": self.refund_requested_at.isoformat() + "Z" if self.refund_requested_at else None,
            "refundUpdatedAt": self.refund_updated_at.isoformat() + "Z" if self.refund_updated_at else None,
            "refundNotes": self.refund_notes,
            "traveler": {
                "name": self.traveler_name,
                "phone": self.traveler_phone,
                "phoneVerified": self.traveler_phone_verified,
                "email": self.traveler_email,
            },
            "details": self.details or {},
        }


class Coupon(Base):
    __tablename__ = "coupons"

    code = Column(String(50), primary_key=True)
    type = Column(String(10), nullable=False)   # percent | flat
    value = Column(Float, nullable=False)
    valid_from = Column(String(10), nullable=False)  # YYYY-MM-DD
    valid_to = Column(String(10), nullable=False)
    max_uses = Column(Integer, nullable=False, default=1)
    used = Column(Integer, nullable=False, default=0)

    def to_dict(self):
        return {
            "code": self.code, "type": self.type, "value": self.value,
            "from": self.valid_from, "to": self.valid_to,
            "max": self.max_uses, "used": self.used,
        }


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    tag = Column(String(255))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "tag": self.tag,
            "active": self.active,
        }


class MarkupConfig(Base):
    """Single-row table holding the platform's current default markup."""
    __tablename__ = "markup_config"

    id = Column(Integer, primary_key=True, default=1)
    type = Column(String(10), nullable=False, default="percent")
    value = Column(Float, nullable=False, default=8.0)

    def to_dict(self):
        return {"type": self.type, "value": self.value}


class Agent(Base):
    __tablename__ = "agents"

    agent_id = Column(String(32), primary_key=True)  # e.g. TBZAGT260917A8XQ2

    entity_type = Column(String(20), nullable=False)   # individual | company
    name = Column(String(255), nullable=False)          # company name or individual full name
    registered_address = Column(String(500), nullable=False)

    phone = Column(String(20), nullable=False)
    phone_verified = Column(Boolean, default=False)

    email = Column(String(255), nullable=False, unique=True)
    email_verified = Column(Boolean, default=False)
    email_verification_code = Column(String(10))
    email_verification_expires = Column(DateTime)

    gst_no = Column(String(20))   # format-validated, not verified against GSTN
    pan_no = Column(String(10))

    password_hash = Column(String(255), nullable=False)

    # pending_verification -> pending_approval -> approved | rejected | suspended
    status = Column(String(30), nullable=False, default="pending_verification")

    session_token = Column(String(64))
    session_token_expires = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)

    def to_dict(self, include_sensitive=False):
        d = {
            "agentId": self.agent_id,
            "entityType": self.entity_type,
            "name": self.name,
            "registeredAddress": self.registered_address,
            "phone": self.phone,
            "phoneVerified": self.phone_verified,
            "email": self.email,
            "emailVerified": self.email_verified,
            "gstNo": self.gst_no,
            "panNo": self.pan_no,
            "status": self.status,
            "createdAt": self.created_at.isoformat() + "Z" if self.created_at else None,
            "approvedAt": self.approved_at.isoformat() + "Z" if self.approved_at else None,
        }
        return d


class AdminUser(Base):
    """A named employee account. Created only via the master ADMIN_API_KEY
    (never through the public site) so account creation itself can't be
    reached by a customer, and every admin action can be attributed to a
    real person instead of a shared secret."""
    __tablename__ = "admin_users"

    admin_id = Column(String(32), primary_key=True)  # e.g. TBZADM260917A8XQ2
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="active")  # active | disabled

    session_token = Column(String(64))
    session_token_expires = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "adminId": self.admin_id,
            "name": self.name,
            "email": self.email,
            "status": self.status,
            "createdAt": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class AuditLog(Base):
    """Append-only record of every admin/employee action. Never updated or
    deleted by the application — that's what makes it usable as an audit
    trail rather than just a status field that gets overwritten."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_type = Column(String(20), nullable=False)   # admin_user | master_key
    actor_id = Column(String(32))                       # admin_id, or null for master_key
    actor_name = Column(String(255), nullable=False)     # employee name, or "Master key"
    action = Column(String(100), nullable=False)          # e.g. "coupon.create"
    target = Column(String(255))                            # e.g. the coupon code affected
    details = Column(JSON)                                    # before/after or other context
    ip_address = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "actorType": self.actor_type,
            "actorId": self.actor_id,
            "actorName": self.actor_name,
            "action": self.action,
            "target": self.target,
            "details": self.details,
            "ip": self.ip_address,
            "ts": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class FlightProviderLog(Base):
    """One row per provider per search — this is what makes 'evaluate
    the providers later and decide which to keep' an answerable question
    instead of just a hope. Without this, there'd be no data to actually
    compare providers against: how often each one responds in time, how
    fast, whether it ever actually offers the winning (cheapest) price,
    or just adds latency and noise to every search.

    Deliberately NOT linked to a specific Booking — most searches never
    become a booking at all, and provider evaluation needs the FULL
    search volume (including ones nobody booked), not just the sliver
    that converted."""
    __tablename__ = "flight_provider_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    search_id = Column(String(40), index=True, nullable=False)  # groups all providers' rows for one search
    from_code = Column(String(10))
    to_code = Column(String(10))
    search_date = Column(String(10))  # the travel date searched for (YYYY-MM-DD), not this row's created_at

    provider = Column(String(30), index=True, nullable=False)
    responded = Column(Boolean, nullable=False)
    error = Column(String(255))
    offer_count = Column(Integer, default=0)
    lowest_fare = Column(Integer)
    won_any_offer = Column(Boolean, default=False)  # did this provider have the cheapest price for ANY deduplicated result

    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "searchId": self.search_id,
            "fromCode": self.from_code, "toCode": self.to_code, "searchDate": self.search_date,
            "provider": self.provider, "responded": self.responded, "error": self.error,
            "offerCount": self.offer_count, "lowestFare": self.lowest_fare, "wonAnyOffer": self.won_any_offer,
            "ts": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class CabProviderLog(Base):
    """Same purpose as FlightProviderLog, mirrored exactly — one row per
    supplier per search, so evaluating cab suppliers later is answerable
    from real data. One extra dimension flight logging doesn't have:
    won_on_reliability — a supplier can "win" a category either by being
    the outright cheapest, or by being routed to despite NOT being
    cheapest, because it was within the reliability-override threshold
    (see cab_providers.RELIABILITY_OVERRIDE_THRESHOLD). Tracking this
    separately matters for evaluation: a supplier winning mostly on
    reliability overrides rather than price is a genuinely different
    signal than one winning on price alone."""
    __tablename__ = "cab_provider_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    search_id = Column(String(40), index=True, nullable=False)
    pickup = Column(String(255))
    drop_location = Column(String(255))
    pickup_datetime = Column(String(40))

    supplier = Column(String(30), index=True, nullable=False)
    responded = Column(Boolean, nullable=False)
    error = Column(String(255))
    quote_count = Column(Integer, default=0)
    lowest_landed_cost = Column(Float)
    won_any_category = Column(Boolean, default=False)
    won_on_reliability = Column(Boolean, default=False)  # won despite NOT being the cheapest for that category

    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "searchId": self.search_id,
            "pickup": self.pickup, "dropLocation": self.drop_location, "pickupDatetime": self.pickup_datetime,
            "supplier": self.supplier, "responded": self.responded, "error": self.error,
            "quoteCount": self.quote_count, "lowestLandedCost": self.lowest_landed_cost,
            "wonAnyCategory": self.won_any_category, "wonOnReliability": self.won_on_reliability,
            "ts": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class CabDispatch(Base):
    """Phase 2: driver assignment and notification status for a booked
    cab transfer. Deliberately real, indexed columns rather than a JSON
    blob — the safety-net job needs to QUERY 'which bookings have a
    pickup in the next 60-90 minutes with no driver yet', which needs
    real columns to filter on, not a scan through opaque JSON."""
    __tablename__ = "cab_dispatches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(32), index=True, nullable=False)
    category = Column(String(30))
    pickup_datetime = Column(String(40), index=True)  # ISO string — indexed, the safety-net job's core filter

    driver_name = Column(String(255))
    driver_phone = Column(String(20))
    driver_rating = Column(Float)
    vehicle_model = Column(String(100))
    vehicle_registration = Column(String(30))
    vehicle_color = Column(String(30))
    live_tracking_url = Column(String(500))
    dispatch_received_at = Column(DateTime)  # when a driver was actually assigned — null until then

    whatsapp_sent = Column(Boolean, default=False)
    whatsapp_sent_at = Column(DateTime)
    email_sent = Column(Boolean, default=False)
    email_sent_at = Column(DateTime)

    safety_net_last_checked_at = Column(DateTime)
    escalated_to_ops = Column(Boolean, default=False)  # true once both a poll AND its fallback have failed to find a driver

    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "bookingId": self.booking_id, "category": self.category, "pickupDatetime": self.pickup_datetime,
            "driverName": self.driver_name, "driverPhone": self.driver_phone, "driverRating": self.driver_rating,
            "vehicleModel": self.vehicle_model, "vehicleRegistration": self.vehicle_registration, "vehicleColor": self.vehicle_color,
            "liveTrackingUrl": self.live_tracking_url,
            "dispatchReceivedAt": self.dispatch_received_at.isoformat() + "Z" if self.dispatch_received_at else None,
            "whatsappSent": self.whatsapp_sent, "whatsappSentAt": self.whatsapp_sent_at.isoformat() + "Z" if self.whatsapp_sent_at else None,
            "emailSent": self.email_sent, "emailSentAt": self.email_sent_at.isoformat() + "Z" if self.email_sent_at else None,
            "escalatedToOps": self.escalated_to_ops,
            "ts": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class CallbackRequest(Base):
    """Lead capture for high-revenue product interest (currently: holiday
    packages). Captured by the support chat when someone asks about a
    package, instead of the bot trying to sell/answer on its own — a
    real staff callback converts better for a high-value product than
    a canned chat answer, hence the deliberate exception."""
    __tablename__ = "callback_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), nullable=False)
    context = Column(String(500))          # what they asked about, for the callback agent
    source = Column(String(50), default="chat_holiday_package")
    status = Column(String(20), nullable=False, default="new")  # new | contacted | closed
    created_at = Column(DateTime, default=datetime.utcnow)
    contacted_at = Column(DateTime)

    def to_dict(self):
        return {
            "id": self.id,
            "phone": self.phone,
            "context": self.context,
            "source": self.source,
            "status": self.status,
            "createdAt": self.created_at.isoformat() + "Z" if self.created_at else None,
            "contactedAt": self.contacted_at.isoformat() + "Z" if self.contacted_at else None,
        }


class BookingAttempt(Base):
    """Logs EVERY booking journey — not just successful ones. This is
    deliberately a separate table from Booking (which only ever holds
    confirmed bookings), because a comprehensive audit/fraud-analysis
    trail needs to see attempts that never became a booking at all
    (partner rejected it) and attempts that eventually succeeded only
    after retries (worth knowing about — repeated persistence failures
    followed by success is a signal of infrastructure flakiness worth
    investigating, even though the customer's booking itself is fine).

    A row that ultimately succeeded is linked to its real Booking via
    final_booking_id — so it naturally shows up in both places: this
    table (as part of the complete attempts history, including its
    retry count) and the Booking table (as the actual confirmed
    booking), joined by that one field. Nothing needs to be duplicated
    or synced between the two; they're just related by that link."""
    __tablename__ = "booking_attempts"

    attempt_id = Column(String(32), primary_key=True)  # e.g. TBZATT260922A8XQ2K

    kind = Column(String(20), nullable=False)  # flight | hotel | itinerary
    title = Column(String(255))
    net_fare = Column(Integer)
    role = Column(String(20))  # customer | agent

    traveler_name = Column(String(255))
    traveler_phone = Column(String(20))
    traveler_email = Column(String(255))
    details = Column(JSON)

    partner_outcome = Column(String(20))       # confirmed | rejected
    persistence_attempts = Column(Integer, default=0)  # how many POST /api/bookings tries this took
    persistence_outcome = Column(String(20))   # success | error | uncertain | not_attempted

    final_status = Column(String(20), nullable=False)  # succeeded | failed_partner | failed_persistence
    final_booking_id = Column(String(32))       # set only if final_status == succeeded — links to Booking
    error_detail = Column(String(500))

    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "attemptId": self.attempt_id,
            "kind": self.kind,
            "title": self.title,
            "netFare": self.net_fare,
            "role": self.role,
            "traveler": {
                "name": self.traveler_name,
                "phone": self.traveler_phone,
                "email": self.traveler_email,
            },
            "details": self.details or {},
            "partnerOutcome": self.partner_outcome,
            "persistenceAttempts": self.persistence_attempts,
            "persistenceOutcome": self.persistence_outcome,
            "finalStatus": self.final_status,
            "finalBookingId": self.final_booking_id,
            "errorDetail": self.error_detail,
            "createdAt": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


def ensure_schema_up_to_date(engine):
    """THE FIX for 'column bookings.payment_mode does not exist' on the
    live database, and — more importantly — for the whole CLASS of bug
    that caused it: Base.metadata.create_all() below only creates
    tables that don't exist yet. It does NOT add new columns to a table
    that already exists. Since 'bookings' already existed in the live
    database from before payment_mode/payment_reference were added to
    this model, every deploy since then silently did nothing for that
    table — the code and the live schema quietly drifted apart, and
    every booking attempt failed with exactly this error.

    This project has no Alembic (a known, previously-flagged gap — see
    the backend README). Rather than requiring a manual ALTER TABLE
    every time a column gets added to an existing table (easy to
    forget, exactly what happened here), this runs automatically on
    every startup and closes the gap itself.

    DELIBERATELY CONSERVATIVE, because this runs against a real,
    already-populated database: it only ever ADDS a column or index
    that's missing. It never drops, renames, or alters an existing
    one, and it never adds a NOT NULL constraint (even if the model
    declares one) — Postgres can't retroactively invent a value for
    rows that already exist, and forcing that constraint here would
    make the ALTER itself fail outright rather than heal the schema.
    A column that genuinely needs to be NOT NULL on a live table needs
    a real, deliberate migration with a backfill plan, not this.

    Idempotent — safe to run on every single startup, not just once.
    Postgres's own IF NOT EXISTS handles "nothing to do" atomically,
    rather than this code trying to diff schemas itself and risking a
    bug in that diffing logic on a real database.

    Each statement runs in its OWN short-lived transaction, deliberately
    — not one shared transaction for the whole function. In Postgres, a
    single failed statement aborts the ENTIRE transaction it's part of,
    and every subsequent statement in that same transaction then fails
    too, even perfectly valid ones — a per-statement try/except alone
    doesn't protect against that if they all share one connection/
    transaction. Isolating each statement means one unusual column type
    this code doesn't handle well can only ever block that one column,
    never silently take out every other column/table this function was
    also supposed to fix in the same run."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    columns_added = 0
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # a genuinely new table — create_all() already handles this case correctly
        for column in table.columns:
            col_type = column.type.compile(dialect=engine.dialect)
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{column.name}" {col_type}'
                    ))
            except Exception as e:
                print(f"WARNING: could not ensure column {table.name}.{column.name} exists: {e}")
                continue
            columns_added += 1
            if column.index and not column.primary_key:
                # Mirrors SQLAlchemy's own default index-naming
                # convention (ix_<table>_<column>) so this doesn't
                # create a second, differently-named duplicate index
                # alongside one create_all() may have already made.
                index_name = f"ix_{table.name}_{column.name}"
                try:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table.name}" ("{column.name}")'
                        ))
                except Exception as e:
                    print(f"WARNING: could not ensure index {index_name} exists: {e}")
    print(f"SCHEMA: checked {len(existing_tables)} existing table(s), verified/added {columns_added} column(s) across all of them")


def make_session(database_url: str):
    """Create the engine + tables (if not already present) and return a session factory."""
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    ensure_schema_up_to_date(engine)
    return sessionmaker(bind=engine)
