"""Runtime configuration for the Wallac bridge and Run Builder.

All secrets come from runtime environment variables — never from config
files or the repository. :class:`BridgeConfig` validates required values
at startup and carries only settings used by the active direct-submit services.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
                 eLabFTW-lambdabiolab/docs/automation-integrations.md
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- Environment variable names --------------------------------------------

# eLabFTW service API key (dedicated bridge key, NOT a human admin key)
ENV_ELABFTW_URL = "WALLAC_ELABFTW_URL"
ENV_ELABFTW_API_KEY = "WALLAC_ELABFTW_API_KEY"
ENV_ELABFTW_VERIFY_TLS = "WALLAC_ELABFTW_VERIFY_TLS"

# vm-agent REST API (the instrument microservice)
ENV_VM_AGENT_URL = "WALLAC_VM_AGENT_URL"
ENV_VM_AGENT_TOKEN = "WALLAC_VM_AGENT_TOKEN"  # noqa: S105  # Env name, not a token.

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

# --- Defaults ---------------------------------------------------------------

DEFAULT_ELABFTW_URL = "https://localhost:3148"
DEFAULT_ELABFTW_VERIFY_TLS = True
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
    vm_agent_url: str
    vm_agent_token: str
    dry_run: bool = False
    cors_origins: list[str] = field(default_factory=list)
    require_auth: bool = False

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

        return cls(
            elabftw_url=e.get(ENV_ELABFTW_URL, DEFAULT_ELABFTW_URL).rstrip("/"),
            elabftw_api_key=api_key,
            elabftw_verify_tls=_parse_bool(
                e.get(ENV_ELABFTW_VERIFY_TLS, ""), DEFAULT_ELABFTW_VERIFY_TLS
            ),
            vm_agent_url=e.get(ENV_VM_AGENT_URL, DEFAULT_VM_AGENT_URL).rstrip("/"),
            vm_agent_token=e.get(ENV_VM_AGENT_TOKEN, "").strip(),
            dry_run=_parse_bool(e.get(ENV_DRY_RUN, ""), False),
            cors_origins=cors_origins,
            require_auth=_parse_bool(e.get(ENV_REQUIRE_AUTH, ""), False),
        )


def _parse_bool(value: str, default: bool) -> bool:
    """Parse a boolean env value. Empty string returns default."""
    v = value.strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")
