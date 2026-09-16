# Physician Directory

Zennya national physician registry (ZO-74993) — extracted from
`zennya-dev/medical-service` into its own service at David's direction
(2026-09-16). Professional identifiers only — no PHI, not vault-gated.

## Surface (slice 1, extracted verbatim from medical-service)

* `GET  /physicians` — search by name / PRC / specialty / society (rate-limited)
* `GET  /physicians/{id}` — one physician with society memberships
* `POST /physicians/seed` — idempotent bulk upsert keyed on PRC no.
  (loader contract ZO-75023; internal-key gated)
* `POST /physicians/{id}/claim-transition` — claim/verification state
  machine (internal-key gated)
* `GET /health`, `GET /metrics`

## Extraction provenance

All slice code is copied verbatim from medical-service main as of
`edcada68` (2026-09-16) except:

* `app/models/models_audit.py` — `MedicalAuditEvent` extracted from
  `models_medical_core.py` (the only core model this slice used).
* `app/services/services_audit.py` — `stable_payload_hash` inlined
  (originally imported from `services_fhir_projection`).
* `alembic/versions/0001_physicians_registry.py` — the registry DDL
  (parent's `0015`) plus the audit table (parent's early `0001`),
  chained as this service's first migration.
* `app/main.py` — slim entrypoint (physicians router only).
* `.github/workflows/deploy.yml` — pin-bump + Argo sync copied from
  medical-service with the ZO-79387 layer-5 fix baked in (Argo refresh
  uses the documented `GET ?refresh=normal`; the parent's broken
  `POST /refresh` is not reproduced here).

## Config

Env vars are inherited from the parent service's contract
(`DATABASE_URL`, `MEDICAL_INTERNAL_API_KEY`, `SERVICE_NAME`,
`API_ROOT_PATH`, `DATABASE_AUTO_CREATE_SCHEMA`) so ops provisioning
stays uniform.

## Run

    uvicorn app.main:app --host 0.0.0.0 --port 8135

## Test

    python -m pytest -n auto
