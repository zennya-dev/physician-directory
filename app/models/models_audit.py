"""Audit event model (extracted from medical-service models_medical_core).

The physician directory records read audits and state-change context on
``medical_audit_events``. The table name is kept identical to the parent
service's audit surface so cross-service audit queries stay uniform; the
physical table lives in THIS service's database.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


class MedicalAuditEvent(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "medical_audit_events"

    request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_roles_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_status: Mapped[str] = mapped_column(String(80), nullable=False)
