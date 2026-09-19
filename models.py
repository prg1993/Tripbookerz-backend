"""
Database models for Tripbookerz.

Mirrors the data that currently lives only in the browser's localStorage
(see the `state` object in the portal's index.html) — this is the real,
shared, durable version of that same data.
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean, DateTime, JSON
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

    # Route/stay/itinerary specifics — kept as JSON since the shape differs
    # by kind (flight vs hotel vs package), same as the frontend's `details`.
    details = Column(JSON)

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


def make_session(database_url: str):
    """Create the engine + tables (if not already present) and return a session factory."""
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
