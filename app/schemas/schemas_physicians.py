"""Pydantic schemas for the National physician registry (ZO-74993).

Walking-skeleton shape contract. Professional identifiers only — no
PHI. The loader contract ZO-75023 feeds ``PhysicianSeedIn`` batches
into the seed route; read endpoints return ``PhysicianOut`` /
``PhysicianListOut`` mirroring the entity shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.models_physicians import (
    CLAIM_STATUSES,
    VERIFICATION_STATUSES,
)


# ---------------------------------------------------------------------------
# Society membership
# ---------------------------------------------------------------------------


class PhysicianSocietyMembershipIn(BaseModel):
    society_code: str = Field(min_length=1, max_length=80)
    society_name: str = Field(min_length=1, max_length=255)
    membership_number: str | None = Field(default=None, max_length=120)
    member_since: datetime | None = None
    member_until: datetime | None = None
    metadata_json: dict[str, Any] | None = None


class PhysicianSocietyMembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    society_code: str
    society_name: str
    membership_number: str | None
    member_since: datetime | None
    member_until: datetime | None
    metadata_json: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Physician write / read shapes
# ---------------------------------------------------------------------------


class PhysicianSeedIn(BaseModel):
    """One row for ``POST /physicians/seed`` — keyed on PRC no."""

    prc_number: str = Field(min_length=1, max_length=40)
    first_name: str = Field(min_length=1, max_length=120)
    middle_name: str | None = Field(default=None, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    name_suffix: str | None = Field(default=None, max_length=40)
    specialty: str | None = Field(default=None, max_length=120)
    sub_specialty: str | None = Field(default=None, max_length=120)
    clinic_name: str | None = Field(default=None, max_length=255)
    clinic_address: str | None = Field(default=None, max_length=512)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=40)
    society_memberships: list[PhysicianSocietyMembershipIn] = Field(
        default_factory=list
    )
    metadata_json: dict[str, Any] | None = None


class PhysicianSeedBatchIn(BaseModel):
    rows: list[PhysicianSeedIn] = Field(min_length=1)


class PhysicianSeedBatchOut(BaseModel):
    inserted: int
    updated: int
    unchanged: int
    total: int


class PhysicianClaimTransitionIn(BaseModel):
    """Move a physician through claim_status.

    ``to_claim_status`` must be one of the four claim_status
    enumerations. ``evidence_reference`` is REQUIRED on a transition
    into ``verified`` so the verification trail is durable.
    """

    to_claim_status: str = Field(min_length=1, max_length=20)
    to_verification_status: str | None = Field(default=None, max_length=20)
    evidence_reference: str | None = Field(default=None, max_length=512)
    actor_type: str | None = Field(default=None, max_length=40)
    actor_id: str | None = Field(default=None, max_length=120)


class PhysicianOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    prc_number: str
    first_name: str
    middle_name: str | None
    last_name: str
    name_suffix: str | None
    specialty: str | None
    sub_specialty: str | None
    clinic_name: str | None
    clinic_address: str | None
    contact_email: str | None
    contact_phone: str | None
    claim_status: str
    verification_status: str
    evidence_reference: str | None
    verified_at: datetime | None
    verified_by_actor_id: str | None
    invited_at: datetime | None
    claimed_at: datetime | None
    claimed_by_actor_id: str | None
    society_memberships: list[PhysicianSocietyMembershipOut] = Field(
        default_factory=list
    )
    metadata_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class PhysicianListOut(BaseModel):
    items: list[PhysicianOut]
    count: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Validators surfaced for the router / service
# ---------------------------------------------------------------------------


def assert_valid_claim_status(value: str) -> str:
    if value not in CLAIM_STATUSES:
        raise ValueError(
            f"invalid claim_status {value!r}; expected one of {CLAIM_STATUSES}"
        )
    return value


def assert_valid_verification_status(value: str) -> str:
    if value not in VERIFICATION_STATUSES:
        raise ValueError(
            "invalid verification_status "
            f"{value!r}; expected one of {VERIFICATION_STATUSES}"
        )
    return value