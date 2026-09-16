from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


@dataclass(frozen=True)
class GrailsBinaryResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


class GrailsMedicalClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.grails_api_base_url
        self.timeout = settings.grails_api_timeout_seconds

    async def get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.get(path, headers=headers)
            response.raise_for_status()
            return response.json()

    async def get_binary(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> GrailsBinaryResponse:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.get(path, headers=headers, params=params)
            return GrailsBinaryResponse(
                status_code=response.status_code,
                content=response.content,
                headers=dict(response.headers),
            )

    async def post_json(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(path, headers=headers, params=params)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            return response.status_code, payload


class GrailsWritebackConfigError(RuntimeError):
    """Raised when the Grails writeback env contract is not satisfied.

    Deliberately an exception rather than a skip: the write path is
    fail-loud — a misconfigured deployment must surface 500s on the
    write route instead of silently dropping provider findings.
    """


@dataclass(frozen=True)
class GrailsVerifiedPrincipal:
    """The principal resolved from a verified Grails ``X-Auth-Token``.

    ``provider_id`` is the integer legacy provider id (the value compared
    against ``legacy_provider_id`` by ``assert_provider_scope``); ``principal_type``
    is the Grails-reported principal type (defaults to ``"provider"``).
    """

    provider_id: int
    principal_type: str = "provider"


class GrailsTokenVerifyClient:
    """Verifies an opaque doctor-session ``X-Auth-Token`` against Grails.

    Confirmed contract (ZO-63421 bridge, live-probed in ZO-63502):
    ``GET {base}/api/1/auth/verify`` with the ``X-Auth-Token`` header set to
    the doctor-session token — the token is both the credential under test
    and the request auth. A ``200`` response carries the verified principal::

        {"user_id": <int>, "profile_id": null|int,
         "corporate_partner_id": null|int, "clinic_partner_id": null|int}

    ``user_id`` IS the authenticated legacy provider id. ``POST`` to the
    same path is a different operation (204, no body) and is never used
    here. An unset ``GRAILS_AUTH_VERIFY_BASE`` (with no
    ``GRAILS_WRITEBACK_BASE`` fallback) raises
    ``GrailsTokenVerifyUnavailableError`` — the X-Auth-Token path is
    unavailable, never silently allowed.
    """

    def __init__(self) -> None:
        settings = get_settings()
        base_url = (settings.grails_auth_verify_base_url or settings.grails_writeback_base_url or "").rstrip("/")
        if not base_url:
            raise GrailsTokenVerifyUnavailableError(
                "GRAILS_AUTH_VERIFY_BASE is not configured; the Grails "
                "X-Auth-Token path cannot be verified"
            )
        self.base_url = base_url
        self.timeout = settings.grails_api_timeout_seconds

    async def verify_token(self, token: str) -> GrailsVerifiedPrincipal:
        """Verify ``token`` against Grails and return the resolved principal.

        Raises ``GrailsTokenVerifyError`` on a non-200 response, a transport
        error, or a body without an integer ``user_id`` so the caller can
        map it to a 401.
        """
        path = "/api/1/auth/verify"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            try:
                response = await client.get(path, headers={"X-Auth-Token": token})
            except httpx.HTTPError as exc:
                raise GrailsTokenVerifyError("Grails auth verify transport failure") from exc
        if response.status_code != 200:
            raise GrailsTokenVerifyError(f"Grails auth verify rejected token: {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        # Exact verified envelope: user_id IS the authenticated provider id
        # (e.g. 1218). Nothing else in the body authorises a principal.
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise GrailsTokenVerifyError("Grails auth verify returned no provider id")
        return GrailsVerifiedPrincipal(provider_id=user_id, principal_type="provider")


class GrailsTokenVerifyUnavailableError(RuntimeError):
    """Raised when the Grails auth-verify endpoint is not configured."""


class GrailsTokenVerifyError(RuntimeError):
    """Raised when a Grails ``X-Auth-Token`` cannot be verified to a provider."""


class GrailsWritebackClient:
    """Client for the legacy Grails consult-results writeback endpoint.

    Contract (medical-service PR #790): ``POST {base}/api/1/internal/
    consultations/{legacy_consultation_id}/results`` authenticated with a
    Grails service-account session token in ``X-Auth-Token`` — the endpoint
    is ``@Secured(IS_AUTHENTICATED_REMEMBERED)``, so the medical internal
    key is NOT accepted there. Returns ``(status_code, payload)`` where the
    Grails payload is ``{result: {...}, replayed: bool}``.
    """

    def __init__(self) -> None:
        settings = get_settings()
        # Fail loud at construction time: an unset GRAILS_WRITEBACK_BASE or
        # GRAILS_WRITEBACK_TOKEN is a configuration error, never a silent
        # skip. The route turns GrailsWritebackConfigError into a 500.
        self.base_url = (settings.grails_writeback_base_url or "").rstrip("/")
        if not self.base_url:
            raise GrailsWritebackConfigError(
                "GRAILS_WRITEBACK_BASE is not configured; consult findings "
                "writeback cannot proceed"
            )
        self.token = settings.grails_writeback_token or ""
        if not self.token:
            raise GrailsWritebackConfigError(
                "GRAILS_WRITEBACK_TOKEN is not configured; consult findings "
                "writeback cannot proceed"
            )
        self.timeout = settings.grails_api_timeout_seconds

    async def post_consult_result(
        self,
        legacy_consultation_id: int,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        path = f"/api/1/internal/consultations/{legacy_consultation_id}/results"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(
                path,
                json=body,
                headers={"X-Auth-Token": self.token},
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload
