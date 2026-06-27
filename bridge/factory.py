"""Factory for wiring bridge components from configuration.

Creates all bridge components (eLabFTW client, vm-agent client, MDB client,
validation service, protocol manager, analysis pipeline, spool, orchestrator)
from a :class:`~bridge.config.BridgeConfig`.

This module is the single place where dependency wiring happens. The bridge
daemon (``main.py``) and tests both use this factory to create consistent,
correctly-wired component graphs.
"""

from __future__ import annotations

import logging
from typing import Any

from .analysis import AnalysisPipeline
from .config import BridgeConfig
from .elabftw import ElabftwClient
from .execution import ExecutionOrchestrator
from .generated_protocols import GeneratedProtocolManager
from .remote_mdb_client import RemoteMdbClient
from .spool import ResultSpool
from .validation import ValidationService
from .vm_agent_client import VmAgentClient

logger = logging.getLogger(__name__)


def create_elabftw_client(config: BridgeConfig) -> ElabftwClient:
    """Create an eLabFTW API client from config."""
    return ElabftwClient(
        base_url=config.elabftw_url,
        api_key=config.elabftw_api_key,
        verify_tls=False,  # dev instance uses self-signed certs
        automation_job_category=config.elabftw_category,
    )


def create_vm_agent_client(config: BridgeConfig) -> VmAgentClient:
    """Create a vm-agent HTTP client from config."""
    return VmAgentClient(
        base_url=config.vm_agent_url,
        token=config.vm_agent_token,
    )


def create_remote_mdb_client(config: BridgeConfig) -> RemoteMdbClient:
    """Create a RemoteMdbClient for MDB operations via vm-agent HTTP."""
    return RemoteMdbClient(
        base_url=config.vm_agent_url,
        token=config.vm_agent_token,
    )


def create_protocol_manager(config: BridgeConfig) -> GeneratedProtocolManager:
    """Create a GeneratedProtocolManager with a RemoteMdbClient.

    The protocol manager is only functional when the vm-agent has
    ``WALLAC_ENABLE_PROTOCOL_AUTHORING=true`` set. The manager itself
    checks this flag via its env dict.
    """
    mdb_client = create_remote_mdb_client(config)
    return GeneratedProtocolManager(mdb_client=mdb_client)


def create_validation_service(
    config: BridgeConfig,
    elabftw_client: ElabftwClient,
    vm_agent_client: VmAgentClient,
) -> ValidationService:
    """Create a ValidationService with eLabFTW and vm-agent clients."""
    return ValidationService(
        elabftw_client=elabftw_client,
        vm_agent_client=vm_agent_client,
    )


def create_spool(config: BridgeConfig) -> ResultSpool:
    """Create a ResultSpool from config."""
    return ResultSpool(spool_dir=config.spool_dir)


def create_orchestrator(config: BridgeConfig) -> ExecutionOrchestrator:
    """Create a fully-wired ExecutionOrchestrator from config.

    This is the main factory function. It wires:
    - ElabftwClient (eLabFTW API)
    - VmAgentClient (instrument REST API)
    - RemoteMdbClient → GeneratedProtocolManager (MDB protocol generation)
    - ValidationService (signed bundle verification)
    - AnalysisPipeline (result analysis)
    - ResultSpool (write-back resilience)
    """
    elabftw = create_elabftw_client(config)
    vm_agent = create_vm_agent_client(config)
    protocols = create_protocol_manager(config)
    validation = create_validation_service(config, elabftw, vm_agent)
    spool = create_spool(config)

    return ExecutionOrchestrator(
        elabftw_client=elabftw,
        vm_agent_client=vm_agent,
        validation_service=validation,
        protocol_manager=protocols,
        analysis_pipeline=AnalysisPipeline(),
        spool=spool,
    )


def create_intake(config: BridgeConfig) -> Any:
    """Create a JobIntake from config.

    Returns a :class:`~bridge.intake.JobIntake` wired with an
    :class:`ElabftwClient` and the bridge identity from config.
    """
    from .intake import JobIntake

    elabftw = create_elabftw_client(config)
    return JobIntake(
        client=elabftw,
        bridge_identity=config.bridge_identity,
        live_monitor_url_base=config.live_monitor_url_base,
    )
