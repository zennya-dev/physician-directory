from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.models_audit import MedicalAuditEvent


def stable_payload_hash(payload: Any) -> str:
    """Deterministic JSON hash — same shape as fhir_projection.stable_payload_hash.

    The original lives in ``app.services.services_fhir_projection`` of
    ``zennya-dev/medical-service``; this copy keeps the audit surface
    stable in the extracted physician-directory without pulling in the
    rest of the fhir machinery.
    """

    def _default(obj: Any) -> Any:
        if isinstance(obj, (set, frozenset)):
            return sorted(obj)
        if isinstance(obj, bytes):
            return obj.hex()
        raise TypeError(f"not serialisable: {type(obj).__name__}")

    encoded = json.dumps(
        payload,
        default=_default,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_medical_read_audit(
    session: Session,
    *,
    request: Request,
    route: str,
    resource_type: str,
    resource_id: str,
    result_status: str,
    actor_type: str | None = None,
    actor_id: str | None = None,
    actor_source: str | None = None,
    action: str = "read",
) -> None:
    actor_context: dict[str, Any] = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "actor_source": actor_source,
    }
    payload_context = {
        "route": route,
        "method": request.method,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "result_status": result_status,
    }
    session.add(
        MedicalAuditEvent(
            request_id=_request_id(request),
            actor_user_id=_actor_user_id(actor_id),
            actor_roles_hash=stable_payload_hash(actor_context),
            route=route,
            method=request.method,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            payload_hash=stable_payload_hash(payload_context),
            result_status=result_status,
        )
    )
    session.commit()


def _request_id(request: Request) -> str | None:
    return request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID")


def _actor_user_id(actor_id: str | None) -> int | None:
    if actor_id is None:
        return None
    try:
        return int(actor_id)
    except ValueError:
        return None
