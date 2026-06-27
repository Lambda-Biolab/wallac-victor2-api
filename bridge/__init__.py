"""Wallac bridge — direct-submit HTTP API for instrument execution.

Sits between the Run Builder (intent surface) and the vm-agent REST API
(instrument control).  Jobs arrive via HTTP POST, not eLabFTW polling.
The bridge owns runtime state, signature verification, live dashboard,
controlled abort, recovery, and eLabFTW write-back.

eLabFTW is the archive and audit trail — not the job queue, intent surface,
or runtime gatekeeper.

See: docs/architecture-direct-submit.md
"""
