import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class UuidPrimaryKeyMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LegacyIdMixin:
    legacy_table: Mapped[str | None] = mapped_column(String(120), nullable=True)
    legacy_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legacy_source: Mapped[str] = mapped_column(String(120), default="zennya-grails-server", nullable=False)
    source_system: Mapped[str] = mapped_column(String(80), default="grails", nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
