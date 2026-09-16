"""National physician registry routers (ZO-74993).

Routes:

* ``GET    /physicians``              — search by name / PRC / specialty / society
* ``GET    /physicians/{id}``         — fetch one physician (with memberships)
* ``POST   /physicians/seed``         — idempotent bulk upsert (loader contract ZO-75023)
* ``POST   `` — not exposed in the walking skeleton; transitions are driven
  by the loader contract and the operator admin tool, not the public API.
  Transitions land here via the internal ``require_internal_api_key``
  dependency in a follow-up slice.

Auth model:
* GET endpoints are open to gateway-authenticated callers; the rate
  limiter bounds per-caller traffic on search.
* The seed route is gated by ``require_internal_api_key`` so only
  service-to-service loader calls can mutate the registry in this
  slice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.schemas_physicians import (
    PhysicianClaimTransitionIn,
    PhysicianListOut,
    PhysicianOut,
    PhysicianSeedBatchIn,
    PhysicianSeedBatchOut,
)
from app.security import require_internal_api_key
from app.services.services_physicians import (
    PhysicianClaimTransitionError,
    get_physician,
    physician_search_rate_limit_check,
    search_physicians,
    transition_physician_claim,
    upsert_physicians_batch,
)


router = APIRouter(prefix="/physicians", tags=["physicians"])


def _rate_limit_caller_key(request: Request) -> str:
    actor_id = request.headers.get("X-Zennya-Actor-Id")
    if actor_id:
        return f"actor:{actor_id}"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    if request.client is not None:
        return f"ip:{request.client.host}"
    return "ip:unknown"


@router.get("", response_model=PhysicianListOut)
def list_or_search_physicians(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=120)] = None,
    prc_number: Annotated[str | None, Query(max_length=40)] = None,
    specialty: Annotated[str | None, Query(max_length=120)] = None,
    society_code: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PhysicianListOut:
    """Search physicians (name / PRC / specialty / society).

    Returns ``{items, count, limit, offset}`` in the same envelope style
    as the existing consultation read routes. Backed by the in-process
    sliding-window rate limiter (per minute, walking-skeleton scope).
    """
    if not physician_search_rate_limit_check(
        caller_key=_rate_limit_caller_key(request)
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="physician search rate limit exceeded",
            headers={"Retry-After": "60"},
        )

    return search_physicians(
        db,
        q=q,
        prc_number=prc_number,
        specialty=specialty,
        society_code=society_code,
        limit=limit,
        offset=offset,
    )


@router.get("/{physician_id}", response_model=PhysicianOut)
def get_physician_by_id(
    physician_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> PhysicianOut:
    """GET one physician by id with society memberships inlined."""
    physician = get_physician(db, physician_id=physician_id)
    if physician is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="physician not found"
        )
    from app.services.services_physicians import _to_physician_out

    return _to_physician_out(physician)


@router.post(
    "/seed",
    response_model=PhysicianSeedBatchOut,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_internal_api_key)],
)
def seed_physicians(
    payload: PhysicianSeedBatchIn,
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> PhysicianSeedBatchOut:
    """Idempotent bulk upsert keyed on PRC no. (loader contract ZO-75023).

    Returns the per-batch counters so the loader can size its replay
    window. The route is internal-key gated; no public / direct-read
    endpoint is exposed here.
    """
    result = upsert_physicians_batch(db, payload)
    db.commit()
    response.headers["X-Physicians-Seeded-Total"] = str(result.total)
    return result


@router.post(
    "/{physician_id}/claim-transition",
    response_model=PhysicianOut,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_internal_api_key)],
)
def post_claim_transition(
    physician_id: str,
    payload: PhysicianClaimTransitionIn,
    db: Annotated[Session, Depends(get_db)],
) -> PhysicianOut:
    """Apply a claim_status / verification_status transition.

    Internal-key gated in this slice. The walking skeleton exposes the
    transition surface so operator tooling can drive the state machine
    while the doctor-web claim flow is built separately.
    """
    try:
        physician = transition_physician_claim(
            db, physician_id=physician_id, payload=payload
        )
    except PhysicianClaimTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    db.commit()
    db.refresh(physician)
    from app.services.services_physicians import _to_physician_out

    return _to_physician_out(physician)