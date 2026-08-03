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

## Secret scanning (two layers)

Two independent scanners run on this repository, and they are not redundant:

- **Gitleaks** scans the working tree and history. The configuration is
  `.github/gitleaks.toml`; the CI workflow is `.github/workflows/secrets.yml`,
  which pins `GITLEAKS_VERSION: "8.30.1"`. The same version is installed
  locally by `make install_tools`. Gitleaks owns the history/PR scan.
- **Detect-secrets** (organization scanners, wired in via the global pre-push
  template at `~/.git-templates/hooks/pre-push`) scans for arbitrary
  high-entropy strings and is allowlisted by `.secrets.baseline`. The
  baseline belongs to detect-secrets, not gitleaks — gitleaks treats it
  as a known-false-positive file via the `paths` allowlist.

If you add a deliberately-encrypted blob or fake-secret test fixture, add
the path to `.github/gitleaks.toml` (for gitleaks) and run
`detect-secrets scan > .secrets.baseline` (for detect-secrets). Both
baselines must be committed.

## Mutation testing

`make mutate` runs `mutmut` against the bridge source. It is **not** part
of the pre-push gate (mutating ~6,000 nodes is too slow for routine
hooks) and is **not** part of CI. Run it manually before opening a pull
request that touches bridge logic, and aim to keep the killed-mutant rate
moving upward — the score is reported by `make mutate-stats`.

## Commits and pull requests

Use Conventional Commits, for example `fix(bridge): handle empty plate` or
`docs: clarify deployment notes`. Pull requests should explain the behavior
change and list the validation commands run.
