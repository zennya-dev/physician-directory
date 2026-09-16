"""Service layer for the National physician registry (ZO-74993).

The service owns:

* idempotent bulk upsert keyed on PRC number for the loader contract
  (``upsert_physicians_batch`` — called by ``POST /physicians/seed``);
* search by name / PRC / specialty / society with pagination
  (``search_physicians``);
* lookup by id (``get_physician``);
* claim / verification state transitions, with the append-only
  ``PhysicianWriteLog`` journal written inside the same transaction
  (``transition_physician_claim``);
* a small in-process rate limiter for the search endpoint
  (``PHYSICIAN_SEARCH_RATE_LIMIT`` — set conservatively for dev; a
  proper Redis-backed limiter is out of scope for the walking
  skeleton).

Every mutation writes a ``PhysicianWriteLog`` row and ALSO a
``MedicalAuditEvent`` so the existing parity/audit pipelines stay
unbroken.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.models_audit import MedicalAuditEvent
from app.models.models_physicians import (
    CLAIM_STATUS_UNCLAIMED,
    CLAIM_STATUSES,
    CLAIM_STATUS_VERIFIED,
    Physician,
    PhysicianSocietyMembership,
    PhysicianWriteLog,
    VERIFICATION_STATUS_UNVERIFIED,
    VERIFICATION_STATUSES,
    VERIFICATION_STATUS_VERIFIED,
)
from app.schemas.schemas_physicians import (
    PhysicianClaimTransitionIn,
    PhysicianListOut,
    PhysicianOut,
    PhysicianSeedBatchIn,
    PhysicianSeedBatchOut,
    PhysicianSeedIn,
    PhysicianSocietyMembershipOut,
    assert_valid_claim_status,
    assert_valid_verification_status,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_physician_out(physician: Physician) -> PhysicianOut:
    memberships = [
        PhysicianSocietyMembershipOut.model_validate(m)
        for m in physician.society_memberships
    ]
    return PhysicianOut(
        id=physician.id,
        prc_number=physician.prc_number,
        first_name=physician.first_name,
        middle_name=physician.middle_name,
        last_name=physician.last_name,
        name_suffix=physician.name_suffix,
        specialty=physician.specialty,
        sub_specialty=physician.sub_specialty,
        clinic_name=physician.clinic_name,
        clinic_address=physician.clinic_address,
        contact_email=physician.contact_email,
        contact_phone=physician.contact_phone,
        claim_status=physician.claim_status,
        verification_status=physician.verification_status,
        evidence_reference=physician.evidence_reference,
        verified_at=physician.verified_at,
        verified_by_actor_id=physician.verified_by_actor_id,
        invited_at=physician.invited_at,
        claimed_at=physician.claimed_at,
        claimed_by_actor_id=physician.claimed_by_actor_id,
        society_memberships=memberships,
        metadata_json=physician.metadata_json,
        created_at=physician.created_at,
        updated_at=physician.updated_at,
    )


# ---------------------------------------------------------------------------
# Append-only write log helper — discipline lives here
# ---------------------------------------------------------------------------


def record_physician_write_event(
    session: Session,
    *,
    physician: Physician,
    event_type: str,
    claim_status_before: str | None,
    claim_status_after: str | None,
    verification_status_before: str | None,
    verification_status_after: str | None,
    actor_type: str | None,
    actor_id: str | None,
    actor_source: str | None,
    evidence_reference: str | None,
    payload_json: dict[str, Any] | None,
) -> PhysicianWriteLog:
    """Insert one ``PhysicianWriteLog`` row.

    This is the ONLY entry point for writing to ``physician_write_log``.
    Service-layer callers MUST NOT ``session.add(PhysicianWriteLog(...))``
    directly so the append-only contract is auditable in one place.
    The ``MedicalAuditEvent`` row is written in the same call so the
    medical-service parity pipeline stays consistent.
    """
    write_log = PhysicianWriteLog(
        physician_id=physician.id,
        event_type=event_type,
        claim_status_before=claim_status_before,
        claim_status_after=claim_status_after,
        verification_status_before=verification_status_before,
        verification_status_after=verification_status_after,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_source=actor_source,
        evidence_reference=evidence_reference,
        payload_json=payload_json,
        created_at=_utcnow(),
    )
    session.add(write_log)

    audit_event = MedicalAuditEvent(
        request_id=None,
        actor_user_id=_coerce_actor_user_id(actor_id),
        actor_roles_hash=None,
        route="physicians/registry",
        method="EVENT",
        resource_type="Physician",
        resource_id=physician.id,
        action=event_type,
        payload_hash=None,
        result_status="success",
    )
    session.add(audit_event)
    return write_log


def _coerce_actor_user_id(actor_id: str | None) -> int | None:
    if actor_id is None:
        return None
    try:
        return int(actor_id)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Loader contract ZO-75023 — idempotent bulk upsert
# ---------------------------------------------------------------------------


def upsert_physicians_batch(
    session: Session,
    payload: PhysicianSeedBatchIn,
    *,
    actor_type: str = "loader",
    actor_id: str | None = None,
    actor_source: str | None = "zo-75023",
) -> PhysicianSeedBatchOut:
    """Idempotent upsert keyed on ``prc_number``.

    A row already present for a PRC no. is updated in place — its
    ``updated_at`` moves on change. An unchanged payload row (every
    column equal) is reported in the ``unchanged`` counter so the
    loader can size a no-op replay.
    """
    inserted = 0
    updated = 0
    unchanged = 0

    for row in payload.rows:
        outcome = _upsert_single(session, row)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

    session.flush()

    total = len(payload.rows)
    return PhysicianSeedBatchOut(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        total=total,
    )


def _upsert_single(session: Session, row: PhysicianSeedIn) -> str:
    existing = session.scalar(
        select(Physician).where(Physician.prc_number == row.prc_number)
    )

    seed_dict = row.model_dump(exclude={"society_memberships"})
    membership_payloads = [
        m.model_dump() for m in row.society_memberships
    ]

    if existing is None:
        physician = Physician(
            prc_number=row.prc_number,
            first_name=row.first_name,
            middle_name=row.middle_name,
            last_name=row.last_name,
            name_suffix=row.name_suffix,
            specialty=row.specialty,
            sub_specialty=row.sub_specialty,
            clinic_name=row.clinic_name,
            clinic_address=row.clinic_address,
            contact_email=row.contact_email,
            contact_phone=row.contact_phone,
            claim_status=CLAIM_STATUS_UNCLAIMED,
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
            metadata_json=row.metadata_json,
        )
        session.add(physician)
        session.flush()  # populate physician.id for FK + write log

        _replace_memberships(session, physician, membership_payloads)

        record_physician_write_event(
            session,
            physician=physician,
            event_type="seed_insert",
            claim_status_before=None,
            claim_status_after=physician.claim_status,
            verification_status_before=None,
            verification_status_after=physician.verification_status,
            actor_type="loader",
            actor_id=None,
            actor_source="zo-75023",
            evidence_reference=None,
            payload_json={"prc_number": row.prc_number, **seed_dict},
        )
        return "inserted"

    changed = False
    for field_name, value in seed_dict.items():
        if field_name == "metadata_json":
            if (existing.metadata_json or {}) != (value or {}):
                existing.metadata_json = value
                changed = True
            continue
        if getattr(existing, field_name) != value:
            setattr(existing, field_name, value)
            changed = True

    if _replace_memberships(
        session, existing, membership_payloads
    ):
        changed = True

    if not changed:
        return "unchanged"

    record_physician_write_event(
        session,
        physician=existing,
        event_type="seed_update",
        claim_status_before=existing.claim_status,
        claim_status_after=existing.claim_status,
        verification_status_before=existing.verification_status,
        verification_status_after=existing.verification_status,
        actor_type="loader",
        actor_id=None,
        actor_source="zo-75023",
        evidence_reference=None,
        payload_json={"prc_number": existing.prc_number, **seed_dict},
    )
    return "updated"


def _replace_memberships(
    session: Session,
    physician: Physician,
    payloads: list[dict[str, Any]],
) -> bool:
    """Replace the physician's memberships atomically.

    Returns ``True`` when the membership set changed (added / removed
    / modified). Memberships are deduplicated by ``(society_code)``.
    """
    incoming_by_code = {p["society_code"]: p for p in payloads}
    existing_by_code = {m.society_code: m for m in physician.society_memberships}

    changed = False
    new_codes = set(incoming_by_code.keys())
    old_codes = set(existing_by_code.keys())

    for code in old_codes - new_codes:
        session.delete(existing_by_code[code])
        changed = True

    for code, payload in incoming_by_code.items():
        existing = existing_by_code.get(code)
        if existing is None:
            session.add(
                PhysicianSocietyMembership(
                    physician_id=physician.id,
                    society_code=payload["society_code"],
                    society_name=payload["society_name"],
                    membership_number=payload.get("membership_number"),
                    member_since=payload.get("member_since"),
                    member_until=payload.get("member_until"),
                    metadata_json=payload.get("metadata_json"),
                )
            )
            changed = True
            continue
        for field_name in (
            "society_name",
            "membership_number",
            "member_since",
            "member_until",
            "metadata_json",
        ):
            new_value = payload.get(field_name)
            if _values_differ(getattr(existing, field_name), new_value):
                setattr(existing, field_name, new_value)
                changed = True

    session.flush()
    return changed


def _values_differ(existing: Any, new_value: Any) -> bool:
    """Value equality that survives SQLite's tzinfo stripping on datetime round-trip.

    SQLite stores datetimes as ISO strings and reads them back without
    tzinfo; a tz-aware value written and then read becomes naive. Naive
    vs tz-aware comparison with ``!=`` always returns True, so a
    replayed batch looks like a change. We normalize both sides to
    naive-UTC before comparing.
    """
    if isinstance(existing, datetime) and isinstance(new_value, datetime):
        existing_norm = (
            existing.replace(tzinfo=None)
            if existing.tzinfo is not None
            else existing
        )
        new_norm = (
            new_value.replace(tzinfo=None)
            if new_value.tzinfo is not None
            else new_value
        )
        return existing_norm != new_norm
    if isinstance(existing, dict) or isinstance(new_value, dict):
        return (existing or {}) != (new_value or {})
    return existing != new_value


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


def get_physician(session: Session, *, physician_id: str) -> Physician | None:
    return session.scalar(
        select(Physician)
        .options(selectinload(Physician.society_memberships))
        .where(Physician.id == physician_id)
    )


def search_physicians(
    session: Session,
    *,
    q: str | None = None,
    prc_number: str | None = None,
    specialty: str | None = None,
    society_code: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> PhysicianListOut:
    """Search physicians by name / PRC / specialty / society.

    ``q`` matches against ``first_name``, ``last_name``, ``middle_name``,
    and ``prc_number`` (case-insensitive contains). The other filters
    are exact-match equality.
    """
    stmt = select(Physician).options(
        selectinload(Physician.society_memberships)
    )

    filters = []
    if q:
        like = f"%{q.lower()}%"
        filters.append(
            or_(
                func.lower(Physician.first_name).like(like),
                func.lower(Physician.last_name).like(like),
                func.lower(Physician.middle_name).like(like),
                func.lower(Physician.prc_number).like(like),
            )
        )
    if prc_number:
        filters.append(Physician.prc_number == prc_number)
    if specialty:
        filters.append(Physician.specialty == specialty)
    if society_code:
        filters.append(
            Physician.id.in_(
                select(PhysicianSocietyMembership.physician_id).where(
                    PhysicianSocietyMembership.society_code == society_code
                )
            )
        )

    if filters:
        stmt = stmt.where(and_(*filters))

    stmt = (
        stmt.order_by(Physician.last_name.asc(), Physician.first_name.asc())
        .limit(limit)
        .offset(offset)
    )

    rows = list(session.scalars(stmt))
    return PhysicianListOut(
        items=[_to_physician_out(p) for p in rows],
        count=len(rows),
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Claim / verification transitions
# ---------------------------------------------------------------------------


_ALLOWED_CLAIM_TRANSITIONS: dict[str, frozenset[str]] = {
    CLAIM_STATUS_UNCLAIMED: frozenset(
        {CLAIM_STATUS_UNCLAIMED, "invited"}
    ),
    "invited": frozenset(
        {CLAIM_STATUS_UNCLAIMED, "invited", "claimed"}
    ),
    "claimed": frozenset({"claimed", CLAIM_STATUS_VERIFIED}),
    CLAIM_STATUS_VERIFIED: frozenset({CLAIM_STATUS_VERIFIED}),
}


class PhysicianClaimTransitionError(ValueError):
    pass


def transition_physician_claim(
    session: Session,
    *,
    physician_id: str,
    payload: PhysicianClaimTransitionIn,
) -> Physician:
    """Apply a claim_status (and optionally verification_status) change.

    Validates the transition against the allowed-state machine, writes
    the ``PhysicianWriteLog`` row inside the same transaction, and
    stamps the appropriate timestamp columns on the physician row.
    """
    physician = session.scalar(
        select(Physician).where(Physician.id == physician_id)
    )
    if physician is None:
        raise PhysicianClaimTransitionError("physician not found")

    target_claim = assert_valid_claim_status(payload.to_claim_status)
    target_verification = (
        assert_valid_verification_status(payload.to_verification_status)
        if payload.to_verification_status is not None
        else physician.verification_status
    )

    allowed = _ALLOWED_CLAIM_TRANSITIONS.get(physician.claim_status, frozenset())
    if target_claim not in allowed:
        raise PhysicianClaimTransitionError(
            "illegal claim_status transition "
            f"{physician.claim_status!r} -> {target_claim!r}"
        )

    if target_claim == CLAIM_STATUS_VERIFIED and target_verification != VERIFICATION_STATUS_VERIFIED:
        raise PhysicianClaimTransitionError(
            "claim_status=verified requires verification_status=verified"
        )

    if (
        target_verification == VERIFICATION_STATUS_VERIFIED
        and not payload.evidence_reference
    ):
        raise PhysicianClaimTransitionError(
            "verification_status=verified requires evidence_reference"
        )

    before_claim = physician.claim_status
    before_verification = physician.verification_status
    now = _utcnow()

    physician.claim_status = target_claim
    physician.verification_status = target_verification

    if target_claim == "invited" and physician.invited_at is None:
        physician.invited_at = now
    if target_claim == "claimed" and physician.claimed_at is None:
        physician.claimed_at = now
        physician.claimed_by_actor_id = payload.actor_id
    if (
        target_verification == VERIFICATION_STATUS_VERIFIED
        and physician.verified_at is None
    ):
        physician.verified_at = now
        physician.verified_by_actor_id = payload.actor_id

    if payload.evidence_reference:
        physician.evidence_reference = payload.evidence_reference

    record_physician_write_event(
        session,
        physician=physician,
        event_type="claim_transition",
        claim_status_before=before_claim,
        claim_status_after=target_claim,
        verification_status_before=before_verification,
        verification_status_after=target_verification,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        actor_source="direct",
        evidence_reference=payload.evidence_reference,
        payload_json={
            "to_claim_status": target_claim,
            "to_verification_status": target_verification,
        },
    )

    session.flush()
    return physician


# ---------------------------------------------------------------------------
# In-process rate limiter for /physicians search (walking skeleton)
# ---------------------------------------------------------------------------


PHYSICIAN_SEARCH_RATE_LIMIT_PER_MINUTE = 60
_PHYSICIAN_SEARCH_WINDOW_SECONDS = 60.0

_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def physician_search_rate_limit_check(*, caller_key: str) -> bool:
    """Sliding-window per-caller limiter for the search endpoint.

    Returns ``True`` when the caller is under the configured per-minute
    budget, ``False`` when the caller is rate-limited. The window is
    process-local — the walking skeleton runs single-process; a
    Redis-backed limiter is a future slice.
    """
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets[caller_key]
        cutoff = now - _PHYSICIAN_SEARCH_WINDOW_SECONDS
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= PHYSICIAN_SEARCH_RATE_LIMIT_PER_MINUTE:
            return False
        bucket.append(now)
        return True


def reset_physician_search_rate_limiter() -> None:
    """Test-only — clears the in-process limiter."""
    with _rate_lock:
        _rate_buckets.clear()


# ---------------------------------------------------------------------------
# Misc helpers exported for the test slice
# ---------------------------------------------------------------------------


def enumerate_physician_claim_statuses() -> Iterable[str]:
    return CLAIM_STATUSES


def enumerate_physician_verification_statuses() -> Iterable[str]:
    return VERIFICATION_STATUSES