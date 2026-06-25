"""Wallac eLabFTW bridge — Linux-side service layer.

Sits between eLabFTW (durable operator-intent + provenance surface) and the
vm-agent REST API (instrument control).  The bridge owns runtime state,
signature verification, job claiming, live dashboard, controlled abort,
recovery, and eLabFTW write-back.

See: eLabFTW-lambdabiolab/docs/wallac-plate-reader-integration.md
     eLabFTW-lambdabiolab/docs/automation-integrations.md
"""
