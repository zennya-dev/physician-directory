"""National physician registry models (ZO-74993).

Walking-skeleton for the National physician registry. Professional
identifiers only — no PHI, not vault-gated.

* ``Physician`` — one row per PRC number, the canonical record.
* ``PhysicianSocietyMembership`` — many-to-one linkage of a physician to
  a specialty society (e.g. PCS, PCP) with the member id.
* ``PhysicianWriteLog`` — append-only record of every state-changing
  write to a physician row. The service layer is the only writer, and
  it INSERTs only — no UPDATE / DELETE (enforced by convention; see
  ``services_physicians.record_physician_write_event``).

The loader contract ZO-75023 feeds ``POST /physicians/seed`` to keep
the mirror in lockstep with the PRC National source.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


# ---------------------------------------------------------------------------
# Claim / verification enumerations (kept in sync with the DB CHECK
# constraints on the physicians table).
# ---------------------------------------------------------------------------

CLAIM_STATUS_UNCLAIMED = "unclaimed"
CLAIM_STATUS_INVITED = "invited"
CLAIM_STATUS_CLAIMED = "claimed"
CLAIM_STATUS_VERIFIED = "verified"

CLAIM_STATUSES: tuple[str, ...] = (
    CLAIM_STATUS_UNCLAIMED,
    CLAIM_STATUS_INVITED,
    CLAIM_STATUS_CLAIMED,
    CLAIM_STATUS_VERIFIED,
)

VERIFICATION_STATUS_UNVERIFIED = "unverified"
VERIFICATION_STATUS_PENDING = "pending"
VERIFICATION_STATUS_VERIFIED = "verified"
VERIFICATION_STATUS_REJECTED = "rejected"

VERIFICATION_STATUSES: tuple[str, ...] = (
    VERIFICATION_STATUS_UNVERIFIED,
    VERIFICATION_STATUS_PENDING,
    VERIFICATION_STATUS_VERIFIED,
    VERIFICATION_STATUS_REJECTED,
)


class Physician(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """National physician registry mirror — professional identifiers only.

    Upsert key is ``prc_number`` (Philippine Regulation Commission
    license number). The loader contract ZO-75023 feeds ``POST
    /physicians/seed`` to keep this row in lockstep with the National
    source; every write is journaled on ``physician_write_log``.
    """

    __tablename__ = "physicians"
    __table_args__ = (
        UniqueConstraint("prc_number", name="uq_physicians_prc_number"),
        Index("ix_physicians_last_name_first_name", "last_name", "first_name"),
        Index("ix_physicians_specialty", "specialty"),
        Index("ix_physicians_claim_status", "claim_status"),
        Index("ix_physicians_verification_status", "verification_status"),
    )

    prc_number: Mapped[str] = mapped_column(String(40), nullable=False)
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_suffix: Mapped[str | None] = mapped_column(String(40), nullable=True)
    specialty: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sub_specialty: Mapped[str | None] = mapped_column(String(120), nullable=True)
    clinic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    clinic_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    claim_status: Mapped[str] = mapped_column(String(20), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(20), default=VERIFICATION_STATUS_UNVERIFIED, nullable=False
    )
    evidence_reference: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by_actor_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    invited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by_actor_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    society_memberships: Mapped[list["PhysicianSocietyMembership"]] = relationship(
        back_populates="physician", cascade="all, delete-orphan"
    )


class PhysicianSocietyMembership(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Society membership (PCS, PCP, etc.) for a physician."""

    __tablename__ = "physician_society_memberships"
    __table_args__ = (
        UniqueConstraint(
            "physician_id",
            "society_code",
            name="uq_physician_society_memberships_physician_society",
        ),
        Index(
            "ix_physician_society_memberships_physician_id", "physician_id"
        ),
        Index(
            "ix_physician_society_memberships_society_code", "society_code"
        ),
    )

    physician_id: Mapped[str] = mapped_column(
        ForeignKey("physicians.id", ondelete="CASCADE"), nullable=False
    )
    society_code: Mapped[str] = mapped_column(String(80), nullable=False)
    society_name: Mapped[str] = mapped_column(String(255), nullable=False)
    membership_number: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    member_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    member_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    physician: Mapped[Physician] = relationship(back_populates="society_memberships")


class PhysicianWriteLog(UuidPrimaryKeyMixin, Base):
    """Append-only write log for physician registry mutations.

    Records who changed what, when, and the before/after status of the
    two state columns (claim_status, verification_status) so downstream
    consumers (loader contract ZO-75023, operator audit) can replay the
    history without joining to ``medical_audit_events``.

    The service layer is the only writer; rows are INSERTed and never
    UPDATED / DELETED. There is no DB-level trigger enforcing append-only
    because SQLite (used in dev / tests) does not enforce triggers as
    PostgreSQL does — discipline lives in the service helper.
    """

    __tablename__ = "physician_write_log"
    __table_args__ = (
        Index(
            "ix_physician_write_log_physician_id_created_at",
            "physician_id",
            "created_at",
        ),
        Index("ix_physician_write_log_event_type", "event_type"),
    )

    physician_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    claim_status_before: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    claim_status_after: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    verification_status_before: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    verification_status_after: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    actor_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    evidence_reference: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Append-only — write-log rows have a ``created_at`` but no
    # ``updated_at`` (rows are never modified after insertion).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
