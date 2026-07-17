# Quality gates for the maintained stacks (docs/102).
# vm-agent/agent.py is excluded from complexity (Windows COM code with
# inherently complex API handlers; bridge/ is the maintained stack).
GATED := bridge/elabftw.py bridge/signature.py bridge/intake.py \
         bridge/models.py bridge/errors.py bridge/lifecycle.py bridge/abort.py \
         bridge/writeback.py bridge/dashboard.py bridge/config.py bridge/secrets_check.py \
         bridge/canonical.py bridge/schemas.py bridge/designer.py bridge/designer_app.py \
         bridge/validation.py bridge/generated_protocols.py bridge/analysis.py \
         bridge/vm_agent_client.py bridge/spool.py bridge/execution.py \
         bridge/jobs.py bridge/bridge_app.py bridge/executor.py

.PHONY: validate format test typecheck complexity setup_dev

validate:  ## lint + typecheck + format-check + tests
	ruff check .
	pyright bridge/
	ruff format --check .
	pytest -q

format:  ## auto-fix lint + format
	ruff check . --fix
	ruff format .

test:  ## run the unit tests
	pytest -q

typecheck:  ## static type check (pyright, bridge/ only — vm-agent is Windows/comtypes)
	pyright bridge/

complexity:  ## cognitive complexity (informational only — not gated)
	@echo "Complexity check is informational only. Run: complexipy bridge/ -mx 15"

setup_dev:  ## install the pre-commit hooks
	uvx pre-commit install --hook-type pre-commit --hook-type commit-msg
