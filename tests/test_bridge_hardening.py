"""Configuration tests for bridge authentication and network hardening."""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge.bridge_app import create_bridge_app
from bridge.config import (
    ENV_ELABFTW_API_KEY,
    ENV_ELABFTW_CA_BUNDLE,
    BridgeConfig,
    ConfigError,
)
from bridge.designer_app import create_designer_app
from bridge.elabftw import build_ssl_context
from bridge.jobs import JobManager

CERT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "certs"


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
        env={
            ENV_ELABFTW_API_KEY: "5-key",
            "WALLAC_ENV": "test",
            "WALLAC_ELABFTW_VERIFY_TLS": value,
        }
    )
    assert config.elabftw_verify_tls is False


def test_config_warns_when_tls_verification_is_disabled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="bridge.config"):
        BridgeConfig.from_env(
            env={
                ENV_ELABFTW_API_KEY: "5-key",
                "WALLAC_ENV": "dev",
                "WALLAC_ELABFTW_VERIFY_TLS": "0",
            }
        )

    assert "emergency diagnostics" in caplog.text
    assert "regular operation" in caplog.text


def test_config_rejects_invalid_boolean() -> None:
    with pytest.raises(ConfigError, match="Invalid boolean"):
        BridgeConfig.from_env(
            env={ENV_ELABFTW_API_KEY: "5-key", "WALLAC_ELABFTW_VERIFY_TLS": "ture"}
        )


def test_config_defaults_to_secure_environment() -> None:
    config = BridgeConfig.from_env(env={ENV_ELABFTW_API_KEY: "5-key"})
    assert config.wallac_env == "production"


@pytest.mark.parametrize("environment", ["staging", "prod", "production"])
def test_config_rejects_tls_disable_in_secure_environments(environment: str) -> None:
    with pytest.raises(ConfigError, match="only allowed"):
        BridgeConfig.from_env(
            env={
                ENV_ELABFTW_API_KEY: "5-key",
                "WALLAC_ENV": environment,
                "WALLAC_ELABFTW_VERIFY_TLS": "0",
            }
        )

    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-key",
            "WALLAC_ENV": "dev",
            "WALLAC_ELABFTW_VERIFY_TLS": "0",
        }
    )
    assert config.wallac_env == "dev"
    assert config.elabftw_verify_tls is False
    assert config.elabftw_ca_bundle is None


def test_config_rejects_tls_disable_in_production() -> None:
    with pytest.raises(ConfigError, match="only allowed"):
        BridgeConfig.from_env(
            env={
                ENV_ELABFTW_API_KEY: "5-key",
                "WALLAC_ENV": "production",
                "WALLAC_ELABFTW_VERIFY_TLS": "0",
            }
        )


def test_config_rejects_bundle_with_tls_disabled(tmp_path) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_text("not used", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot be set"):
        BridgeConfig.from_env(
            env={
                ENV_ELABFTW_API_KEY: "5-key",
                ENV_ELABFTW_CA_BUNDLE: str(bundle),
                "WALLAC_ELABFTW_VERIFY_TLS": "0",
            }
        )


def test_ssl_context_rejects_invalid_pem_bundle(tmp_path) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_text(
        "-----BEGIN CERTIFICATE-----\n!!!notbase64!!!\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Invalid"):
        build_ssl_context(verify_tls=True, ca_bundle=str(bundle))


def test_ssl_context_accepts_ca_true_bundle() -> None:
    ca_count_before = ssl.create_default_context().cert_store_stats()["x509_ca"]
    context = build_ssl_context(
        verify_tls=True,
        ca_bundle=str(CERT_FIXTURES / "ca-true.crt"),
    )
    assert context.cert_store_stats()["x509_ca"] >= ca_count_before + 1


def test_ssl_context_accepts_already_trusted_ca(monkeypatch: pytest.MonkeyPatch) -> None:
    ca_bundle = str(CERT_FIXTURES / "ca-true.crt")
    original_create_default_context = ssl.create_default_context

    def create_context_with_ca() -> ssl.SSLContext:
        context = original_create_default_context()
        context.load_verify_locations(cafile=ca_bundle)
        return context

    preloaded_ca_count = create_context_with_ca().cert_store_stats()["x509_ca"]
    # Regression guard: patch only the populated default context; the bare
    # SSLContext used for semantic validation must remain empty.
    monkeypatch.setattr("bridge.elabftw.ssl.create_default_context", create_context_with_ca)

    context = build_ssl_context(verify_tls=True, ca_bundle=ca_bundle)
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] >= preloaded_ca_count


def test_ssl_context_accepts_multi_ca_bundle(tmp_path) -> None:
    ca_count_before = ssl.create_default_context().cert_store_stats()["x509_ca"]
    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_text(
        (CERT_FIXTURES / "ca-true.crt").read_text(encoding="utf-8")
        + (CERT_FIXTURES / "ca-true-2.crt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    context = build_ssl_context(verify_tls=True, ca_bundle=str(bundle))
    assert context.cert_store_stats()["x509_ca"] >= ca_count_before + 2


def test_ssl_context_rejects_ca_false_bundle() -> None:
    with pytest.raises(ConfigError, match="no CA:TRUE trust anchor"):
        build_ssl_context(
            verify_tls=True,
            ca_bundle=str(CERT_FIXTURES / "ca-false.crt"),
        )


def test_designer_rejects_invalid_ca_bundle_at_startup(tmp_path) -> None:
    config = BridgeConfig.from_env(
        env={
            ENV_ELABFTW_API_KEY: "5-key",
            ENV_ELABFTW_CA_BUNDLE: str(tmp_path / "missing.pem"),
        }
    )
    with pytest.raises(ConfigError, match="Invalid eLabFTW CA bundle"):
        create_designer_app(config=config, service=object())


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
