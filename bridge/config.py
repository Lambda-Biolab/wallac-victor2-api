"""Configuration for the Wallac bridge.

Implements issue #6: service identity and dashboard access controls.

All secrets come from runtime environment variables — never from config
files, never committed to the repo.  :class:`BridgeConfig` validates that
required secrets are present and non-empty at startup, and provides the
service identity, session token, and network binding configuration to
the bridge components.

Source contract: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
                 eLabFTW-lambdabiolab/docs/automation-integrations.md
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --- Environment variable names --------------------------------------------

# eLabFTW service API key (dedicated bridge key, NOT a human admin key)
ENV_ELABFTW_URL = "WALLAC_ELABFTW_URL"
ENV_ELABFTW_API_KEY = "WALLAC_ELABFTW_API_KEY"
ENV_ELABFTW_CATEGORY = "WALLAC_ELABFTW_CATEGORY"

# vm-agent REST API (the instrument microservice)
ENV_VM_AGENT_URL = "WALLAC_VM_AGENT_URL"
ENV_VM_AGENT_TOKEN = "WALLAC_VM_AGENT_TOKEN"

# Dashboard session token (optional; if unset, dashboard is open on the LAN)
ENV_DASHBOARD_TOKEN = "WALLAC_DASHBOARD_TOKEN"

# Dashboard network binding
ENV_DASHBOARD_HOST = "WALLAC_DASHBOARD_HOST"
ENV_DASHBOARD_PORT = "WALLAC_DASHBOARD_PORT"

# Bridge identity (for write-back "Claimed by" field)
ENV_BRIDGE_IDENTITY = "WALLAC_BRIDGE_IDENTITY"
ENV_DEVICE_IDENTITY = "WALLAC_DEVICE_IDENTITY"


# --- Defaults ---------------------------------------------------------------

DEFAULT_ELABFTW_URL = "https://localhost:3148"
DEFAULT_ELABFTW_CATEGORY = 9  # Automation Job resource category
DEFAULT_VM_AGENT_URL = "http://192.168.122.203:8420"
DEFAULT_DASHBOARD_HOST = "0.0.0.0"
DEFAULT_DASHBOARD_PORT = 8421
DEFAULT_BRIDGE_IDENTITY = "wallac-bridge"
DEFAULT_DEVICE_IDENTITY = "victor2-unknown"


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
    elabftw_category: int
    vm_agent_url: str
    vm_agent_token: str
    dashboard_token: str
    dashboard_host: str
    dashboard_port: int
    bridge_identity: str
    device_identity: str

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

        return cls(
            elabftw_url=e.get(ENV_ELABFTW_URL, DEFAULT_ELABFTW_URL).rstrip("/"),
            elabftw_api_key=api_key,
            elabftw_category=int(e.get(ENV_ELABFTW_CATEGORY, DEFAULT_ELABFTW_CATEGORY)),
            vm_agent_url=e.get(ENV_VM_AGENT_URL, DEFAULT_VM_AGENT_URL).rstrip("/"),
            vm_agent_token=e.get(ENV_VM_AGENT_TOKEN, "").strip(),
            dashboard_token=e.get(ENV_DASHBOARD_TOKEN, "").strip(),
            dashboard_host=e.get(ENV_DASHBOARD_HOST, DEFAULT_DASHBOARD_HOST),
            dashboard_port=int(e.get(ENV_DASHBOARD_PORT, DEFAULT_DASHBOARD_PORT)),
            bridge_identity=e.get(ENV_BRIDGE_IDENTITY, DEFAULT_BRIDGE_IDENTITY),
            device_identity=e.get(ENV_DEVICE_IDENTITY, DEFAULT_DEVICE_IDENTITY),
        )

    @property
    def dashboard_requires_auth(self) -> bool:
        """True if the dashboard session token is set (auth enforced)."""
        return bool(self.dashboard_token)

    @property
    def live_monitor_url_base(self) -> str:
        """Base URL for the Live Monitor field written to eLabFTW."""
        return f"http://{self.dashboard_host}:{self.dashboard_port}"

    def redacted(self) -> dict[str, str]:
        """Return a dict of config values with secrets masked.

        Safe to log or include in diagnostics.  Secrets are replaced with
        ``***REDACTED***``.
        """
        return {
            "elabftw_url": self.elabftw_url,
            "elabftw_api_key": "***REDACTED***",
            "elabftw_category": str(self.elabftw_category),
            "vm_agent_url": self.vm_agent_url,
            "vm_agent_token": "***REDACTED***" if self.vm_agent_token else "(unset)",
            "dashboard_token": "***REDACTED***" if self.dashboard_token else "(unset)",
            "dashboard_host": self.dashboard_host,
            "dashboard_port": str(self.dashboard_port),
            "bridge_identity": self.bridge_identity,
            "device_identity": self.device_identity,
        }
