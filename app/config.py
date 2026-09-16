import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    service_name: str = field(default_factory=lambda: os.getenv("SERVICE_NAME", "medical-service"))
    root_path: str = field(default_factory=lambda: os.getenv("API_ROOT_PATH", "/medicalapi"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./medical.db"))
    grails_api_base_url: str = field(
        default_factory=lambda: os.getenv("GRAILS_API_BASE_URL", "http://dev.api.zennya.com").rstrip("/")
    )
    grails_api_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("GRAILS_API_TIMEOUT_SECONDS", "30"))
    )
    medical_read_mirror_enabled: bool = field(default_factory=lambda: _env_bool("MEDICAL_READ_MIRROR_ENABLED", False))
    medical_write_bridge_enabled: bool = field(default_factory=lambda: _env_bool("MEDICAL_WRITE_BRIDGE_ENABLED", False))
    fhir_projection_enabled: bool = field(default_factory=lambda: _env_bool("FHIR_PROJECTION_ENABLED", True))
    phi_payload_logging_enabled: bool = field(default_factory=lambda: _env_bool("PHI_PAYLOAD_LOGGING_ENABLED", False))
    database_auto_create_schema: bool = field(default_factory=lambda: _env_bool("DATABASE_AUTO_CREATE_SCHEMA", False))
    medical_internal_api_key: str = field(default_factory=lambda: os.getenv("MEDICAL_INTERNAL_API_KEY", ""))
    medical_direct_read_gateway_key: str = field(
        default_factory=lambda: os.getenv("MEDICAL_DIRECT_READ_GATEWAY_KEY", "")
    )

    # Grails consult-results writeback (ZO-57086, medical-service PR #790
    # contract). Unset values fail loud on the write route — the writeback
    # is synchronous and mandatory, never silently skipped.
    grails_writeback_base_url: str = field(
        default_factory=lambda: os.getenv("GRAILS_WRITEBACK_BASE", "")
    )
    grails_writeback_token: str = field(
        default_factory=lambda: os.getenv("GRAILS_WRITEBACK_TOKEN", "")
    )

    # Grails doctor-session token verification (ZO-63421). The doctor-react
    # app authenticates its providers by an opaque Grails session token sent
    # as ``X-Auth-Token``; medical-service verifies it against Grails
    # ``/api/1/auth/verify`` before authorising the consult routes. When
    # GRAILS_AUTH_VERIFY_BASE is unset, the verify client falls back to
    # GRAILS_WRITEBACK_BASE; if neither is set the X-Auth-Token path is
    # UNAVAILABLE and 401s (never a silent allow).
    grails_auth_verify_base_url: str = field(
        default_factory=lambda: os.getenv("GRAILS_AUTH_VERIFY_BASE", "")
    )

    # Pre-admin vitals hard-block evaluator (ZO-34869).
    # Source of truth: MED 4280483841 (Doc Kevin calibration). These env-driven
    # values keep the clinical threshold editable without a code deploy while
    # Doc Kevin still has open EMS calibration work.
    pre_admin_vitals_systolic_threshold: float = field(
        default_factory=lambda: float(os.getenv("PRE_ADMIN_VITALS_SYSTOLIC_THRESHOLD", "180"))
    )
    pre_admin_vitals_diastolic_threshold: float = field(
        default_factory=lambda: float(os.getenv("PRE_ADMIN_VITALS_DIASTOLIC_THRESHOLD", "120"))
    )
    pre_admin_vitals_threshold_ref: str = field(
        default_factory=lambda: os.getenv("PRE_ADMIN_VITALS_THRESHOLD_REF", "MED 4280483841")
    )
    pre_admin_vitals_threshold_version: str = field(
        default_factory=lambda: os.getenv("PRE_ADMIN_VITALS_THRESHOLD_VERSION", "v1")
    )

    # FDA Circular 2020-037 e-prescription digital signature (ZO-75019).
    # The signing service holds a private RSA-2048 key (PEM, PKCS#8).
    # In dev/test a deterministic ephemeral key is generated; in
    # production this MUST be supplied via env so the signing key is
    # not baked into the image. The ``key_id`` is stamped onto every
    # signature so verifiers can rotate keys without breaking existing
    # audit history.
    prescription_signing_private_key_pem: str = field(
        default_factory=lambda: os.getenv(
            "PRESCRIPTION_SIGNING_PRIVATE_KEY_PEM", ""
        )
    )
    prescription_signing_key_id: str = field(
        default_factory=lambda: os.getenv(
            "PRESCRIPTION_SIGNING_KEY_ID", "dev-prescription-signing-key-v1"
        )
    )
    # Toggle that forces every prescription-group download/email path
    # through the signer. Default ON for dev so the walking skeleton is
    # exercised; production rollout will gate by group origin (see
    # docs/fda-2020-037-element-checklist.md).
    prescription_signing_required: bool = field(
        default_factory=lambda: _env_bool(
            "PRESCRIPTION_SIGNING_REQUIRED", True
        )
    )
    prescription_verification_base_url: str = field(
        default_factory=lambda: os.getenv(
            "PRESCRIPTION_VERIFICATION_BASE_URL",
            "https://dev.api.zennya.com/medicalapi/api/v1/medical-prescriptions/verify",
        )
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
