"""0001 — physicians registry + audit table (ZO-74993).

This is the very first migration on the physician-directory service:
it creates the ``physicians`` and related tables (originally
``0015_physicians_registry`` on the parent medical-service), plus the
``medical_audit_events`` table the service writes to (originally created
much earlier there as part of ``0001_initial_medical_core``).

Both tables are extracted verbatim from the parent's DDL — the
audit-event columns and the physicians table / indexes / check
constraints match the parent's definitions, so a future data migration
out of medical-service would be a row copy, not a schema rewrite.
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_physicians_registry"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # ---- medical_audit_events (extracted from 0001_initial_medical_core) ----
    if not _has_table("medical_audit_events"):
        op.create_table(
            "medical_audit_events",
            sa.Column("request_id", sa.String(length=120), nullable=True),
            sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
            sa.Column("actor_roles_hash", sa.String(length=128), nullable=True),
            sa.Column("route", sa.String(length=255), nullable=False),
            sa.Column("method", sa.String(length=20), nullable=False),
            sa.Column("resource_type", sa.String(length=120), nullable=True),
            sa.Column("resource_id", sa.String(length=120), nullable=True),
            sa.Column("action", sa.String(length=120), nullable=False),
            sa.Column("payload_hash", sa.String(length=128), nullable=True),
            sa.Column("result_status", sa.String(length=80), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    # ---- physicians (extracted from 0015_physicians_registry) ----
    if not _has_table("physicians"):
        op.create_table(
            "physicians",
            sa.Column("prc_number", sa.String(length=40), nullable=False),
            sa.Column("first_name", sa.String(length=120), nullable=False),
            sa.Column("middle_name", sa.String(length=120), nullable=True),
            sa.Column("last_name", sa.String(length=120), nullable=False),
            sa.Column("name_suffix", sa.String(length=40), nullable=True),
            sa.Column("specialty", sa.String(length=120), nullable=True),
            sa.Column("sub_specialty", sa.String(length=120), nullable=True),
            sa.Column("clinic_name", sa.String(length=255), nullable=True),
            sa.Column("clinic_address", sa.String(length=512), nullable=True),
            sa.Column("contact_email", sa.String(length=255), nullable=True),
            sa.Column("contact_phone", sa.String(length=40), nullable=True),
            sa.Column("claim_status", sa.String(length=20), nullable=False),
            sa.Column(
                "verification_status",
                sa.String(length=20),
                nullable=False,
                server_default="unverified",
            ),
            sa.Column("evidence_reference", sa.String(length=512), nullable=True),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verified_by_actor_id", sa.String(length=120), nullable=True),
            sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_by_actor_id", sa.String(length=120), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "prc_number", name="uq_physicians_prc_number"
            ),
            sa.CheckConstraint(
                "claim_status IN ('unclaimed','invited','claimed','verified')",
                name="ck_physicians_claim_status",
            ),
            sa.CheckConstraint(
                "verification_status IN ('unverified','pending','verified','rejected')",
                name="ck_physicians_verification_status",
            ),
        )
        op.create_index(
            "ix_physicians_last_name_first_name",
            "physicians",
            ["last_name", "first_name"],
        )
        op.create_index("ix_physicians_specialty", "physicians", ["specialty"])
        op.create_index(
            "ix_physicians_claim_status", "physicians", ["claim_status"]
        )
        op.create_index(
            "ix_physicians_verification_status",
            "physicians",
            ["verification_status"],
        )

    if not _has_table("physician_society_memberships"):
        op.create_table(
            "physician_society_memberships",
            sa.Column("physician_id", sa.String(length=36), nullable=False),
            sa.Column("society_code", sa.String(length=80), nullable=False),
            sa.Column("society_name", sa.String(length=255), nullable=False),
            sa.Column("membership_number", sa.String(length=120), nullable=True),
            sa.Column("member_since", sa.DateTime(timezone=True), nullable=True),
            sa.Column("member_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["physician_id"], ["physicians.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "physician_id",
                "society_code",
                name="uq_physician_society_memberships_physician_society",
            ),
        )
        op.create_index(
            "ix_physician_society_memberships_physician_id",
            "physician_society_memberships",
            ["physician_id"],
        )
        op.create_index(
            "ix_physician_society_memberships_society_code",
            "physician_society_memberships",
            ["society_code"],
        )

    if not _has_table("physician_write_log"):
        op.create_table(
            "physician_write_log",
            sa.Column("physician_id", sa.String(length=36), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column(
                "claim_status_before",
                sa.String(length=20),
                nullable=True,
            ),
            sa.Column(
                "claim_status_after",
                sa.String(length=20),
                nullable=True,
            ),
            sa.Column(
                "verification_status_before",
                sa.String(length=20),
                nullable=True,
            ),
            sa.Column(
                "verification_status_after",
                sa.String(length=20),
                nullable=True,
            ),
            sa.Column("actor_type", sa.String(length=40), nullable=True),
            sa.Column("actor_id", sa.String(length=120), nullable=True),
            sa.Column("actor_source", sa.String(length=80), nullable=True),
            sa.Column("evidence_reference", sa.String(length=512), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_physician_write_log_physician_id_created_at",
            "physician_write_log",
            ["physician_id", "created_at"],
        )
        op.create_index(
            "ix_physician_write_log_event_type",
            "physician_write_log",
            ["event_type"],
        )


def downgrade() -> None:
    if _has_table("physician_write_log"):
        op.drop_index(
            "ix_physician_write_log_event_type", table_name="physician_write_log"
        )
        op.drop_index(
            "ix_physician_write_log_physician_id_created_at",
            table_name="physician_write_log",
        )
        op.drop_table("physician_write_log")

    if _has_table("physician_society_memberships"):
        op.drop_index(
            "ix_physician_society_memberships_society_code",
            table_name="physician_society_memberships",
        )
        op.drop_index(
            "ix_physician_society_memberships_physician_id",
            table_name="physician_society_memberships",
        )
        op.drop_table("physician_society_memberships")

    if _has_table("physicians"):
        op.drop_index(
            "ix_physicians_verification_status", table_name="physicians"
        )
        op.drop_index("ix_physicians_claim_status", table_name="physicians")
        op.drop_index("ix_physicians_specialty", table_name="physicians")
        op.drop_index(
            "ix_physicians_last_name_first_name", table_name="physicians"
        )
        op.drop_table("physicians")

    if _has_table("medical_audit_events"):
        op.drop_table("medical_audit_events")
