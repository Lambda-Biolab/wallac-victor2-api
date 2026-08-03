# Contribution guide

## Development setup

This repository contains a Linux-side `bridge/` service and a Windows-only
`vm-agent/`. The locked Linux development environment is managed with `uv`:

```bash
make setup_dev
```

This installs dependencies and the pre-commit, commit-message, and pre-push
hooks. Do not bypass hooks with `--no-verify`; fix the reported gate instead.

## Quality gates

Run the complete local gate before opening a pull request:

```bash
make validate
```

The gate runs Ruff, Pyright over `bridge/` and `tests/`, formatting checks,
pytest with the 80% bridge coverage threshold, complexipy (maximum 15),
gitleaks, and Bandit. The pre-push hook additionally checks branch coverage
for changed bridge code before chaining to the organization secret scanners.

Useful focused commands:

```bash
make lint
make typecheck
make test
make coverage
make bandit
make secrets
```

The Windows agent's COM and live-instrument paths require the Windows runtime
and hardware integration environment; they are not part of the Linux unit
coverage gate.

## Commits and pull requests

Use Conventional Commits, for example `fix(bridge): handle empty plate` or
`docs: clarify deployment notes`. Pull requests should explain the behavior
change and list the validation commands run.
