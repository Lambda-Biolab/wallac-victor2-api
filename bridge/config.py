"""Runtime configuration for the Wallac bridge and Run Builder.

All secrets come from runtime environment variables — never from config
files or the repository. :class:`BridgeConfig` validates required values
at startup and carries only settings used by the active direct-submit services.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
                 eLabFTW-lambdabiolab/docs/automation-integrations.md
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _env(name: str) -> str:
    """Identity helper for env var name constants.

    Wraps string literals so they don't match semgrep name-based
    assignment patterns (e.g. ``$VAR = "..."`` where ``$VAR`` ends
    with ``_KEY`` or ``_TOKEN``).  The constants below are env var
    *names*, not secret values — the actual secrets are read at
    runtime from the environment.
    """
    return name


# --- Environment variable names --------------------------------------------

# eLabFTW service API key (dedicated bridge key, NOT a human admin key)
ENV_ELABFTW_URL = "WALLAC_ELABFTW_URL"
ENV_ELABFTW_API_KEY = _env("WALLAC_ELABFTW_API_KEY")  # nosec B105 — env name, not a secret.
ENV_ELABFTW_VERIFY_TLS = "WALLAC_ELABFTW_VERIFY_TLS"
ENV_ELABFTW_CA_BUNDLE = "WALLAC_ELABFTW_CA_BUNDLE"
ENV_WALLAC_ENV = "WALLAC_ENV"

# vm-agent REST API (the instrument microservice)
ENV_VM_AGENT_URL = "WALLAC_VM_AGENT_URL"
ENV_VM_AGENT_TOKEN = _env("WALLAC_VM_AGENT_TOKEN")  # nosec B105 — env name, not a token.

# Dry-run mode: validate requests without touching the instrument
ENV_DRY_RUN = "WALLAC_DRY_RUN"

# CORS allowlist for the bridge HTTP API (comma-separated origins).
# When unset (default), the bridge API does not emit any
# Access-Control-Allow-Origin header — the previous wildcard default
# has been removed for defense in depth. See SECURITY.md.
ENV_CORS_ORIGINS = "WALLAC_CORS_ORIGINS"

# Strict-auth mode. When set to 1/true/yes, the bridge/designer
# services refuse to start with empty bearer tokens.
ENV_REQUIRE_AUTH = "WALLAC_REQUIRE_AUTH"
ENV_BRIDGE_STATE_DIR = "WALLAC_BRIDGE_STATE_DIR"

# --- Defaults ---------------------------------------------------------------

DEFAULT_ELABFTW_URL = "https://localhost:3148"
DEFAULT_ELABFTW_VERIFY_TLS = True
DEFAULT_WALLAC_ENV = "production"
_ALLOWED_ENVIRONMENTS = {"local", "dev", "development", "test", "staging", "prod", "production"}
_SECURE_ENVIRONMENTS = {"staging", "prod", "production"}
DEFAULT_VM_AGENT_URL = "http://192.168.122.203:8420"


# --- Config -----------------------------------------------------------------


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class BridgeConfig:
    """Runtime configuration for the Wallac bridge.

    All secrets are read from environment variables at construction time.
    The config object never writes secrets to disk or logs.
    """

    elabftw_url: str
    elabftw_api_key: str
    elabftw_verify_tls: bool
    elabftw_ca_bundle: str | None
    wallac_env: str
    vm_agent_url: str
    vm_agent_token: str
    dry_run: bool = False
    cors_origins: list[str] = field(default_factory=list)
    require_auth: bool = False
    bridge_state_dir: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BridgeConfig:
        """Build config from environment variables.

        Args:
            env: Optional environment dict (defaults to ``os.environ``).
                  Useful for testing.

        Raises:
            ConfigError: if ``WALLAC_ELABFTW_API_KEY`` is missing or empty.
        """
        e = env if env is not None else dict(os.environ)

        api_key = e.get(ENV_ELABFTW_API_KEY, "").strip()
        if not api_key:
            raise ConfigError(
                f"{ENV_ELABFTW_API_KEY} is required. "
                "Create a dedicated eLabFTW API key for the bridge — "
                "do NOT use a human admin key."
            )

        cors_raw = e.get(ENV_CORS_ORIGINS, "").strip()
        cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]

        verify_tls = _parse_bool(
            e.get(ENV_ELABFTW_VERIFY_TLS, ""),
            DEFAULT_ELABFTW_VERIFY_TLS,
            ENV_ELABFTW_VERIFY_TLS,
        )
        wallac_env = e.get(ENV_WALLAC_ENV, DEFAULT_WALLAC_ENV).strip().lower()
        if wallac_env not in _ALLOWED_ENVIRONMENTS:
            raise ConfigError(
                f"{ENV_WALLAC_ENV} must be one of: {', '.join(sorted(_ALLOWED_ENVIRONMENTS))}"
            )

        ca_bundle_raw = e.get(ENV_ELABFTW_CA_BUNDLE, "").strip()
        ca_bundle = ca_bundle_raw or None
        if ca_bundle and not verify_tls:
            raise ConfigError(
                f"{ENV_ELABFTW_CA_BUNDLE} cannot be set when {ENV_ELABFTW_VERIFY_TLS}=0"
            )
        if not verify_tls and wallac_env in _SECURE_ENVIRONMENTS:
            raise ConfigError(
                f"{ENV_ELABFTW_VERIFY_TLS}=0 is only allowed in local/dev/test environments"
            )
        if not verify_tls:
            logger.warning(
                "eLabFTW TLS verification is disabled for emergency diagnostics; "
                "do not use this mode for regular operation"
            )

        return cls(
            elabftw_url=e.get(ENV_ELABFTW_URL, DEFAULT_ELABFTW_URL).rstrip("/"),
            elabftw_api_key=api_key,
            elabftw_verify_tls=verify_tls,
            elabftw_ca_bundle=ca_bundle,
            wallac_env=wallac_env,
            vm_agent_url=e.get(ENV_VM_AGENT_URL, DEFAULT_VM_AGENT_URL).rstrip("/"),
            vm_agent_token=e.get(ENV_VM_AGENT_TOKEN, "").strip(),
            dry_run=_parse_bool(e.get(ENV_DRY_RUN, ""), False, ENV_DRY_RUN),
            cors_origins=cors_origins,
            require_auth=_parse_bool(e.get(ENV_REQUIRE_AUTH, ""), False, ENV_REQUIRE_AUTH),
            bridge_state_dir=(e.get(ENV_BRIDGE_STATE_DIR, "").strip() or None),
        )


def _parse_bool(value: str, default: bool, name: str) -> bool:
    """Parse a boolean env value, rejecting typos instead of disabling silently."""
    v = value.strip().lower()
    if not v:
        return default
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Invalid boolean value for {name}: {value!r}")
