from dataclasses import dataclass
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.clients.clients_grails_medical import (
    GrailsTokenVerifyClient,
    GrailsTokenVerifyError,
    GrailsTokenVerifyUnavailableError,
)
from app.config import get_settings
from app.database import get_db
from app.services.services_audit import record_medical_read_audit

DIRECT_READ_PRIVILEGED_ACTOR_TYPES = {"admin", "lab", "staff", "provider"}

# Consultation-keyed routes authorize provider actors by matching their
# (integer-parseable) actor id against the consulted row's ``legacy_provider_id``
# on the P1 mirrored tables. See ``docs/direct-client-auth-plan.md`` —
# "Provider actor scope rules".
CONSULT_PROVIDER_SCOPE_MESSAGE = "Consult read provider scope denied"


@dataclass(frozen=True)
class MedicalReadActor:
    actor_type: str
    actor_id: str
    source: str


async def require_internal_api_key(
    provided_key: Annotated[str | None, Header(alias="X-Medical-Internal-Key")] = None,
) -> None:
    if not _internal_key_is_valid(provided_key):
        configured_key = get_settings().medical_internal_api_key
        if not configured_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Medical internal API key is not configured",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid medical internal API key")


async def require_patient_read_access(
    personal_info_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    internal_key: Annotated[str | None, Header(alias="X-Medical-Internal-Key")] = None,
    direct_key: Annotated[str | None, Header(alias="X-Medical-Direct-Read-Key")] = None,
    actor_type: Annotated[str | None, Header(alias="X-Zennya-Actor-Type")] = None,
    actor_id: Annotated[str | None, Header(alias="X-Zennya-Actor-Id")] = None,
    patient_id: Annotated[str | None, Header(alias="X-Zennya-Patient-Id")] = None,
    patient_ids: Annotated[str | None, Header(alias="X-Zennya-Patient-Ids")] = None,
) -> MedicalReadActor:
    if internal_key is not None:
        await require_internal_api_key(internal_key)
        return MedicalReadActor(actor_type="internal", actor_id="medical-internal", source="internal")

    actor = _validate_direct_read_gateway_or_audit_denial(
        db,
        request=request,
        provided_key=direct_key,
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type="MedicalPatient",
        resource_id=str(personal_info_id),
    )
    if actor.actor_type in DIRECT_READ_PRIVILEGED_ACTOR_TYPES:
        return actor

    allowed_patient_ids = _parse_ints([patient_id, patient_ids])
    if personal_info_id not in allowed_patient_ids:
        _record_denied_read(
            db,
            request=request,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            actor_source=actor.source,
            resource_type="MedicalPatient",
            resource_id=str(personal_info_id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Patient read scope denied")
    return actor


async def require_order_read_access(
    legacy_order_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    internal_key: Annotated[str | None, Header(alias="X-Medical-Internal-Key")] = None,
    direct_key: Annotated[str | None, Header(alias="X-Medical-Direct-Read-Key")] = None,
    actor_type: Annotated[str | None, Header(alias="X-Zennya-Actor-Type")] = None,
    actor_id: Annotated[str | None, Header(alias="X-Zennya-Actor-Id")] = None,
    order_ids: Annotated[str | None, Header(alias="X-Zennya-Medical-Order-Ids")] = None,
) -> MedicalReadActor:
    if internal_key is not None:
        await require_internal_api_key(internal_key)
        return MedicalReadActor(actor_type="internal", actor_id="medical-internal", source="internal")

    actor = _validate_direct_read_gateway_or_audit_denial(
        db,
        request=request,
        provided_key=direct_key,
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type="MedicalOrder",
        resource_id=str(legacy_order_id),
    )
    if actor.actor_type in DIRECT_READ_PRIVILEGED_ACTOR_TYPES:
        return actor

    if legacy_order_id not in _parse_ints([order_ids]):
        _record_denied_read(
            db,
            request=request,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            actor_source=actor.source,
            resource_type="MedicalOrder",
            resource_id=str(legacy_order_id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Medical order read scope denied")
    return actor


# ---------------------------------------------------------------------------
# Consultation route family — provider-scope direct read (ZO-57089 + ZO-57088)
# ---------------------------------------------------------------------------
#
# The consultation workflow strangler's P3 routes are keyed by
# ``legacy_provider_id`` (consult history), ``legacy_consult_request_id`` /
# ``legacy_consultation_id`` (detail / findings / attachments / audit-log), or
# ``legacy_attachment_id`` (attachment descriptor). The plan doc's "Provider
# actor scope rules" section defines the scope contract: a provider actor may
# read ONLY rows whose ``legacy_provider_id`` matches their actor id; admin /
# lab / staff actor types pass per ``DIRECT_READ_PRIVILEGED_ACTOR_TYPES``;
# patient actors are rejected (consultation routes are provider-only —
# patient scope for the patient audit-log is handled separately by
# ``require_patient_read_access``).
#
# The dependency intentionally reuses the same gateway trust scheme as
# ``require_patient_read_access`` / ``require_order_read_access``: a valid
# internal key is the service-to-service alternative; the gateway-injected
# direct-read trust key plus ``X-Zennya-Actor-Type`` / ``X-Zennya-Actor-Id`` is
# the client-side credential. The actor id is the string from the header,
# compared against the integer ``legacy_provider_id`` after ``int()`` parsing.
# A provider actor whose id is not a valid int is rejected for the route
# family (no matches are possible).
#
# ZO-63421 adds a THIRD credential: the doctor-react app sends the opaque
# Grails session token as ``X-Auth-Token`` (no direct-read gateway headers).
# When only X-Auth-Token is present it is verified against Grails
# ``/api/1/auth/verify`` and resolved to a provider principal, yielding the
# SAME ``MedicalReadActor(actor_type="provider", actor_id=<provider id>,
# source="grails_token")`` shape the direct-read path yields — so
# ``assert_provider_scope`` (legacy_provider_id match) and
# ``record_medical_read_audit`` (allowed + rejected) apply unchanged.
# Invalid / expired tokens and an unset verify base are 401s, never a
# silent allow. The internal-key and direct-read-gateway paths are
# unchanged and keep their precedence.


async def require_consult_read_access(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    internal_key: Annotated[str | None, Header(alias="X-Medical-Internal-Key")] = None,
    direct_key: Annotated[str | None, Header(alias="X-Medical-Direct-Read-Key")] = None,
    actor_type: Annotated[str | None, Header(alias="X-Zennya-Actor-Type")] = None,
    actor_id: Annotated[str | None, Header(alias="X-Zennya-Actor-Id")] = None,
    auth_token: Annotated[str | None, Header(alias="X-Auth-Token")] = None,
) -> MedicalReadActor:
    """Authorize a consultation-route direct read and return the actor.

    The route function follows up with ``assert_provider_scope`` (or
    ``assert_consult_audit_log_patient_scope``) to enforce the row-level
    ``legacy_provider_id`` match. Returning the actor here decouples the
    gateway check from the per-route scope rule so each consultation route
    can record its own resource type on the audit row.
    """

    if internal_key is not None:
        await require_internal_api_key(internal_key)
        return MedicalReadActor(actor_type="internal", actor_id="medical-internal", source="internal")

    if direct_key is None and auth_token:
        return await _resolve_grails_token_actor_or_audit_denial(
            db,
            request=request,
            auth_token=auth_token,
        )

    actor = _validate_direct_read_gateway_or_audit_denial(
        db,
        request=request,
        provided_key=direct_key,
        actor_type=actor_type,
        actor_id=actor_id,
        resource_type="ConsultFamily",
        # The actual resource_id is supplied by the per-route call to
        # ``assert_provider_scope``; this audit row only records that the
        # gateway trust / actor identity stage was reached for the consult
        # family. Per-row denial / success audit rows include the real id.
        resource_id="gateway",
    )
    if actor.actor_type == "patient":
        _record_denied_read(
            db,
            request=request,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            actor_source=actor.source,
            resource_type="ConsultFamily",
            resource_id="patient-actor-rejected",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CONSULT_PROVIDER_SCOPE_MESSAGE,
        )
    return actor


def parse_provider_actor_id(actor: MedicalReadActor) -> int | None:
    """Return the integer provider id implied by the actor id header, if any.

    A provider actor whose ``X-Zennya-Actor-Id`` is not a valid int is treated
    as "no match possible" by ``assert_provider_scope`` — the act of comparison
    is what authorizes a row, not the parse itself, so an unparseable id is a
    scope miss (403), not a malformed-request (400). The audit row records the
    raw actor id verbatim so an operator can still correlate the attempt.
    """

    try:
        return int(actor.actor_id)
    except (TypeError, ValueError):
        return None


def assert_provider_scope(
    session: Session,
    *,
    request: Request,
    actor: MedicalReadActor,
    route: str,
    resource_type: str,
    resource_id: str,
    row_provider_id: int | None,
) -> None:
    """Reject the consult read if ``row_provider_id`` does not match the actor.

    Routes call this helper AFTER loading the row so the comparison runs
    against the real ``legacy_provider_id`` (the row's "owner provider" per
    the P1 mirror schema). A privileged actor type passes without a
    provider-id match; a provider actor must match; patient actors are
    already rejected upstream by ``require_consult_read_access``.

    On rejection, the helper records a denied PHI-safe read-audit row on
    the same session with the real per-row ``resource_id`` so an operator
    can correlate the attempt to the row that was targeted. The route
    function writes the success-path audit row itself (mirroring the
    patient-scoped consult-audit-log route's pattern).
    """

    _ = route  # kept for symmetry / future use; the helper delegates the
    # audit-row route to ``_record_denied_read`` which derives the route
    # template path from ``request.scope["route"].path``.

    if actor.actor_type in DIRECT_READ_PRIVILEGED_ACTOR_TYPES and actor.actor_type != "provider":
        return
    if actor.source == "internal":
        return

    actor_provider_id = parse_provider_actor_id(actor)
    if row_provider_id is None or actor_provider_id != row_provider_id:
        _record_denied_read(
            session,
            request=request,
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            actor_source=actor.source,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CONSULT_PROVIDER_SCOPE_MESSAGE,
        )


def _internal_key_is_valid(provided_key: str | None) -> bool:
    configured_key = get_settings().medical_internal_api_key
    return bool(configured_key and provided_key and compare_digest(provided_key, configured_key))


def _validate_direct_read_gateway(
    provided_key: str | None,
    actor_type: str | None,
    actor_id: str | None,
) -> MedicalReadActor:
    configured_key = get_settings().medical_direct_read_gateway_key
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Medical direct read gateway key is not configured",
        )
    if not provided_key or not compare_digest(provided_key, configured_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid medical direct read gateway key")
    normalized_actor_type = (actor_type or "").strip().lower()
    normalized_actor_id = (actor_id or "").strip()
    if not normalized_actor_type or not normalized_actor_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing direct read actor identity")
    return MedicalReadActor(actor_type=normalized_actor_type, actor_id=normalized_actor_id, source="direct")


def _validate_direct_read_gateway_or_audit_denial(
    session: Session,
    *,
    request: Request,
    provided_key: str | None,
    actor_type: str | None,
    actor_id: str | None,
    resource_type: str,
    resource_id: str,
) -> MedicalReadActor:
    try:
        return _validate_direct_read_gateway(provided_key, actor_type, actor_id)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            _record_denied_read(
                session,
                request=request,
                actor_type=(actor_type or "").strip().lower() or None,
                actor_id=(actor_id or "").strip() or None,
                actor_source="direct",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        raise


# ---------------------------------------------------------------------------
# Grails doctor-session token (ZO-63421)
# ---------------------------------------------------------------------------


async def _resolve_grails_token_actor_or_audit_denial(
    session: Session,
    *,
    request: Request,
    auth_token: str,
) -> MedicalReadActor:
    """Verify an ``X-Auth-Token`` against Grails and resolve the provider actor.

    Only reached when NO internal key and NO direct-read gateway key are
    present, so the existing credential paths keep their precedence and
    behaviour. On success the actor is the same shape the direct-read path
    yields for a provider (``actor_type="provider"``), so
    ``assert_provider_scope`` enforces the ``legacy_provider_id`` match and
    both success and denied reads are audited identically. Any verification
    failure (invalid / expired token, verify endpoint misconfigured,
    transport error) is a denied audit row + 401 — never a silent allow.
    """

    try:
        principal = await GrailsTokenVerifyClient().verify_token(auth_token)
    except (GrailsTokenVerifyUnavailableError, GrailsTokenVerifyError) as exc:
        _record_denied_read(
            session,
            request=request,
            actor_type="provider",
            actor_id=None,
            actor_source="grails_token",
            resource_type="ConsultFamily",
            resource_id="grails-token-denied",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or unverifiable Grails auth token",
        ) from exc
    return MedicalReadActor(
        actor_type="provider",
        actor_id=str(principal.provider_id),
        source="grails_token",
    )


def _record_denied_read(
    session: Session,
    *,
    request: Request,
    actor_type: str | None,
    actor_id: str | None,
    actor_source: str | None,
    resource_type: str,
    resource_id: str,
) -> None:
    record_medical_read_audit(
        session,
        request=request,
        route=request.scope.get("route").path if request.scope.get("route") else request.url.path,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_source=actor_source,
        result_status="denied",
    )


def _parse_ints(values: list[str | None]) -> set[int]:
    parsed: set[int] = set()
    for value in values:
        for item in (value or "").split(","):
            stripped = item.strip()
            if not stripped:
                continue
            try:
                parsed.add(int(stripped))
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid direct read scope id")
    return parsed
