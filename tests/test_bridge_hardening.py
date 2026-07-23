"""Configuration tests for bridge authentication and network hardening."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bridge.bridge_app import create_bridge_app
from bridge.config import ENV_ELABFTW_API_KEY, BridgeConfig, ConfigError
from bridge.designer_app import create_designer_app
from bridge.jobs import JobManager


def test_config_requires_api_key() -> None:
    with pytest.raises(ConfigError, match=ENV_ELABFTW_API_KEY):
        BridgeConfig.from_env(env={})


def test_config_rejects_empty_api_key() -> None:
    with pytest.raises(ConfigError):
        BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "   "})


def test_config_loads_active_service_settings() -> None:
    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-testkey123",
            "WALLAC_ELABFTW_URL": "https://elab.local:3148/",
            "WALLAC_VM_AGENT_URL": "http://vm-agent.local:8420/",
            "WALLAC_VM_AGENT_TOKEN": "vm-token",
            "WALLAC_DRY_RUN": "true",
        }
    )

    assert config.elabftw_api_key == "5-testkey123"
    assert config.elabftw_url == "https://elab.local:3148"
    assert config.vm_agent_url == "http://vm-agent.local:8420"
    assert config.vm_agent_token == "vm-token"
    assert config.dry_run is True


def test_config_elabftw_verify_tls_defaults_true() -> None:
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    assert config.elabftw_verify_tls is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_config_elabftw_verify_tls_can_be_disabled(value: str) -> None:
    config = BridgeConfig.from_env(
        env={ENV_ELABFTW_API_KEY: "5-key", "WALLAC_ELABFTW_VERIFY_TLS": value}
    )
    assert config.elabftw_verify_tls is False


def test_config_cors_origins_defaults_empty() -> None:
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    assert config.cors_origins == []


def test_config_cors_origins_parses_csv() -> None:
    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-key",
            "WALLAC_CORS_ORIGINS": "http://localhost:8422, https://run.example.com",
        }
    )
    assert config.cors_origins == ["http://localhost:8422", "https://run.example.com"]


def test_config_require_auth_defaults_false() -> None:
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    assert config.require_auth is False


def test_config_require_auth_can_be_enabled() -> None:
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key", "WALLAC_REQUIRE_AUTH": "1"})
    assert config.require_auth is True


def test_bridge_require_auth_rejects_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WALLAC_BRIDGE_TOKEN", raising=False)
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key", "WALLAC_REQUIRE_AUTH": "1"})

    with pytest.raises(RuntimeError, match="WALLAC_BRIDGE_TOKEN is empty"):
        create_bridge_app(config=config)


def test_designer_require_auth_rejects_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WALLAC_DESIGNER_TOKEN", raising=False)
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key", "WALLAC_REQUIRE_AUTH": "1"})

    with pytest.raises(RuntimeError, match="WALLAC_DESIGNER_TOKEN is empty"):
        create_designer_app(config=config, service=object())


def test_designer_events_proxy_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLAC_DESIGNER_TOKEN", "designer-token")
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    app = create_designer_app(config=config, service=object())

    response = TestClient(app).get("/elabftw/events?items_id=1")

    assert response.status_code == 401


def test_bridge_default_cors_emits_no_allow_origin() -> None:
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    manager = JobManager()
    app = create_bridge_app(config=config, job_manager=manager)
    try:
        response = TestClient(app).get("/jobs", headers={"Origin": "https://run.example.com"})
        assert "access-control-allow-origin" not in response.headers
    finally:
        manager.stop_worker()


def test_bridge_cors_allows_configured_origin() -> None:
    origin = "https://run.example.com"
    config = BridgeConfig.from_env(
        env={ENV_ELABFTW_API_KEY: "5-key", "WALLAC_CORS_ORIGINS": origin}
    )
    manager = JobManager()
    app = create_bridge_app(config=config, job_manager=manager)
    try:
        response = TestClient(app).get("/jobs", headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin
    finally:
        manager.stop_worker()
