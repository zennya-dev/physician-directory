"""ZO-74993 slice tests for the National physician registry.

Walking-skeleton coverage:

* ``test_seed_and_get`` — ``POST /physicians/seed`` upserts a row keyed
  on PRC no. and ``GET /physicians/{id}`` returns it with memberships.
* ``test_search`` — ``GET /physicians`` finds the seeded row by PRC,
  by name fragment, by specialty, and by society_code; pagination
  returns ``{items, count, limit, offset}``.
* ``test_claim_transitions_audited`` — claim_status / verification_status
  transitions write a ``PhysicianWriteLog`` row and a ``MedicalAuditEvent``
  row inside the same transaction; illegal transitions 400; verification
  requires ``evidence_reference``.

These are DB+router slice tests that intentionally avoid touching
sibling surfaces (``services_medical_orders``, ``services_prescriptions``,
``services_result_abnormality``).
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models.base import Base
from app.models.models_audit import MedicalAuditEvent
from app.models.models_physicians import (
    Physician,
    PhysicianWriteLog,
)
from app.services.services_physicians import (
    reset_physician_search_rate_limiter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


@pytest.fixture()
def client(session: Session) -> Generator[TestClient, None, None]:
    SessionLocal = sessionmaker(bind=session.bind, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    reset_physician_search_rate_limiter()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        reset_physician_search_rate_limiter()
        get_settings.cache_clear()


def _internal_headers(**overrides: str) -> dict[str, str]:
    headers = {"X-Medical-Internal-Key": "expected-key"}
    headers.update(overrides)
    return headers


def _seed_payload() -> dict:
    return {
        "rows": [
            {
                "prc_number": "PRC-0012345",
                "first_name": "Maria",
                "middle_name": "Santos",
                "last_name": "Reyes",
                "name_suffix": "MD",
                "specialty": "Internal Medicine",
                "sub_specialty": "Cardiology",
                "clinic_name": "Manila Heart Clinic",
                "clinic_address": "Taft Ave, Manila",
                "contact_email": "maria.reyes@example.com",
                "contact_phone": "+639171234567",
                "society_memberships": [
                    {
                        "society_code": "PCS",
                        "society_name": "Philippine College of Surgeons",
                        "membership_number": "PCS-998",
                        "member_since": "2018-03-01T00:00:00Z",
                    },
                    {
                        "society_code": "PCP",
                        "society_name": "Philippine College of Physicians",
                        "membership_number": "PCP-101",
                    },
                ],
                "metadata_json": {"source": "zo-75023-pilot"},
            }
        ]
    }


# ---------------------------------------------------------------------------
# 1) seed + get
# ---------------------------------------------------------------------------


def test_seed_and_get(client: TestClient, session: Session, monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("MEDICAL_INTERNAL_API_KEY", "expected-key")

    seed_response = client.post(
        "/physicians/seed",
        json=_seed_payload(),
        headers=_internal_headers(),
    )
    assert seed_response.status_code == 200, seed_response.text
    seed_body = seed_response.json()
    assert seed_body == {"inserted": 1, "updated": 0, "unchanged": 0, "total": 1}

    physician = session.scalar(
        select(Physician).where(Physician.prc_number == "PRC-0012345")
    )
    assert physician is not None
    physician_id = physician.id

    get_response = client.get(f"/physicians/{physician_id}")
    assert get_response.status_code == 200, get_response.text
    body = get_response.json()
    assert body["prc_number"] == "PRC-0012345"
    assert body["first_name"] == "Maria"
    assert body["last_name"] == "Reyes"
    assert body["claim_status"] == "unclaimed"
    assert body["verification_status"] == "unverified"
    assert {m["society_code"] for m in body["society_memberships"]} == {"PCS", "PCP"}

    # Idempotency — a second seed with the same payload must report
    # ``unchanged: 1`` so the loader contract can size its replay.
    second = client.post(
        "/physicians/seed",
        json=_seed_payload(),
        headers=_internal_headers(),
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body == {"inserted": 0, "updated": 0, "unchanged": 1, "total": 1}


# ---------------------------------------------------------------------------
# 2) search by name / PRC / specialty / society
# ---------------------------------------------------------------------------


def test_search(client: TestClient, session: Session, monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("MEDICAL_INTERNAL_API_KEY", "expected-key")

    # Seed three rows so we can exercise the search filters.
    rows = {
        "rows": [
            {
                "prc_number": "PRC-0012345",
                "first_name": "Maria",
                "last_name": "Reyes",
                "specialty": "Internal Medicine",
                "society_memberships": [
                    {
                        "society_code": "PCS",
                        "society_name": "Philippine College of Surgeons",
                    }
                ],
            },
            {
                "prc_number": "PRC-0099887",
                "first_name": "Juan",
                "last_name": "Dela Cruz",
                "specialty": "Pediatrics",
                "society_memberships": [
                    {
                        "society_code": "PPS",
                        "society_name": "Philippine Pediatric Society",
                    }
                ],
            },
            {
                "prc_number": "PRC-0054321",
                "first_name": "Ana",
                "last_name": "Bautista",
                "specialty": "Internal Medicine",
                "society_memberships": [
                    {
                        "society_code": "PCP",
                        "society_name": "Philippine College of Physicians",
                    }
                ],
            },
        ]
    }
    seed_response = client.post(
        "/physicians/seed", json=rows, headers=_internal_headers()
    )
    assert seed_response.status_code == 200, seed_response.text

    # By PRC no.
    by_prc = client.get("/physicians", params={"prc_number": "PRC-0054321"})
    assert by_prc.status_code == 200, by_prc.text
    body = by_prc.json()
    assert body["count"] == 1
    assert body["items"][0]["prc_number"] == "PRC-0054321"
    assert body["limit"] == 50
    assert body["offset"] == 0

    # By name fragment
    by_name = client.get("/physicians", params={"q": "reyes"})
    assert by_name.status_code == 200, by_name.text
    body = by_name.json()
    assert body["count"] == 1
    assert body["items"][0]["prc_number"] == "PRC-0012345"

    # By specialty
    by_specialty = client.get(
        "/physicians", params={"specialty": "Internal Medicine"}
    )
    assert by_specialty.status_code == 200, by_specialty.text
    body = by_specialty.json()
    assert body["count"] == 2
    assert {r["prc_number"] for r in body["items"]} == {
        "PRC-0012345",
        "PRC-0054321",
    }

    # By society_code
    by_society = client.get("/physicians", params={"society_code": "PPS"})
    assert by_society.status_code == 200, by_society.text
    body = by_society.json()
    assert body["count"] == 1
    assert body["items"][0]["prc_number"] == "PRC-0099887"

    # Pagination — limit=1 + offset walks the ordered list.
    page_1 = client.get("/physicians", params={"limit": 1, "offset": 0})
    page_2 = client.get("/physicians", params={"limit": 1, "offset": 1})
    assert page_1.status_code == 200 and page_2.status_code == 200
    assert page_1.json()["count"] == 1
    assert page_2.json()["count"] == 1
    assert (
        page_1.json()["items"][0]["prc_number"]
        != page_2.json()["items"][0]["prc_number"]
    )


# ---------------------------------------------------------------------------
# 3) claim transitions audited
# ---------------------------------------------------------------------------


def test_claim_transitions_audited(
    client: TestClient, session: Session, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("MEDICAL_INTERNAL_API_KEY", "expected-key")

    seed_response = client.post(
        "/physicians/seed", json=_seed_payload(), headers=_internal_headers()
    )
    assert seed_response.status_code == 200, seed_response.text
    physician = session.scalar(
        select(Physician).where(Physician.prc_number == "PRC-0012345")
    )
    assert physician is not None
    physician_id = physician.id

    # unclaimed -> invited (legal)
    invited = client.post(
        f"/physicians/{physician_id}/claim-transition",
        json={"to_claim_status": "invited"},
        headers=_internal_headers(),
    )
    assert invited.status_code == 200, invited.text
    assert invited.json()["claim_status"] == "invited"
    assert invited.json()["invited_at"] is not None

    # invited -> claimed (legal)
    claimed = client.post(
        f"/physicians/{physician_id}/claim-transition",
        json={
            "to_claim_status": "claimed",
            "actor_id": "user-42",
            "actor_type": "doctor",
        },
        headers=_internal_headers(),
    )
    assert claimed.status_code == 200, claimed.text
    claimed_body = claimed.json()
    assert claimed_body["claim_status"] == "claimed"
    assert claimed_body["claimed_at"] is not None
    assert claimed_body["claimed_by_actor_id"] == "user-42"

    # claimed -> verified WITH evidence (legal) — sets verification_status
    verified = client.post(
        f"/physicians/{physician_id}/claim-transition",
        json={
            "to_claim_status": "verified",
            "to_verification_status": "verified",
            "evidence_reference": "PRC-verify-2026-09-14-001",
            "actor_id": "ops-77",
        },
        headers=_internal_headers(),
    )
    assert verified.status_code == 200, verified.text
    verified_body = verified.json()
    assert verified_body["claim_status"] == "verified"
    assert verified_body["verification_status"] == "verified"
    assert verified_body["evidence_reference"] == "PRC-verify-2026-09-14-001"
    assert verified_body["verified_at"] is not None
    assert verified_body["verified_by_actor_id"] == "ops-77"

    # Illegal transition — verified -> claimed (must 400).
    illegal = client.post(
        f"/physicians/{physician_id}/claim-transition",
        json={"to_claim_status": "claimed"},
        headers=_internal_headers(),
    )
    assert illegal.status_code == 400, illegal.text
    assert "illegal claim_status transition" in illegal.json()["detail"]

    # Append-only audit invariant — there must be three write-log rows
    # (seed_insert, claim_transition x 3) and three MedicalAuditEvent
    # rows (seed_insert, claim_transition x 3), each stamped with the
    # matching actor.
    write_logs = list(
        session.scalars(
            select(PhysicianWriteLog)
            .where(PhysicianWriteLog.physician_id == physician_id)
            .order_by(PhysicianWriteLog.created_at.asc())
        )
    )
    assert len(write_logs) == 4
    assert [w.event_type for w in write_logs] == [
        "seed_insert",
        "claim_transition",
        "claim_transition",
        "claim_transition",
    ]
    assert [w.claim_status_after for w in write_logs] == [
        "unclaimed",
        "invited",
        "claimed",
        "verified",
    ]
    assert [w.verification_status_after for w in write_logs] == [
        "unverified",
        "unverified",
        "unverified",
        "verified",
    ]
    assert write_logs[2].actor_id == "user-42"
    assert write_logs[3].actor_id == "ops-77"

    audit_events = list(
        session.scalars(
            select(MedicalAuditEvent)
            .where(MedicalAuditEvent.resource_id == physician_id)
            .order_by(MedicalAuditEvent.created_at.asc())
        )
    )
    assert len(audit_events) == 4
    assert [a.action for a in audit_events] == [
        "seed_insert",
        "claim_transition",
        "claim_transition",
        "claim_transition",
    ]
    # ``MedicalAuditEvent.actor_user_id`` only stores ints — non-int
    # actor ids (``"user-42"``, ``"ops-77"``) deliberately coerce to
    # ``None`` per the existing service_audit helper. The actor identity
    # is durably recorded on the ``PhysicianWriteLog`` row instead.
    assert audit_events[0].actor_user_id is None
    assert audit_events[2].actor_user_id is None  # user-42 → not an int
    assert audit_events[3].actor_user_id is None  # ops-77 → not an int


def test_verified_transition_requires_evidence(
    client: TestClient, session: Session, monkeypatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("MEDICAL_INTERNAL_API_KEY", "expected-key")

    seed_response = client.post(
        "/physicians/seed", json=_seed_payload(), headers=_internal_headers()
    )
    assert seed_response.status_code == 200, seed_response.text
    physician = session.scalar(
        select(Physician).where(Physician.prc_number == "PRC-0012345")
    )
    assert physician is not None
    physician_id = physician.id

    # Walk to claimed first, then attempt verified without evidence — must 400.
    for to_status in ("invited", "claimed"):
        step = client.post(
            f"/physicians/{physician_id}/claim-transition",
            json={"to_claim_status": to_status},
            headers=_internal_headers(),
        )
        assert step.status_code == 200, step.text

    no_evidence = client.post(
        f"/physicians/{physician_id}/claim-transition",
        json={
            "to_claim_status": "verified",
            "to_verification_status": "verified",
        },
        headers=_internal_headers(),
    )
    assert no_evidence.status_code == 400, no_evidence.text
    assert "evidence_reference" in no_evidence.json()["detail"]