# Quality gates for the maintained stacks (docs/102).
# Tools are run via uv so no global installs are required.
GATED := vm-agent/agent.py vm-agent/lid_watcher.py vm-agent/launch_as_user.py \
         bridge/elabftw.py bridge/signature.py bridge/intake.py \
         bridge/models.py bridge/errors.py bridge/lifecycle.py bridge/abort.py \
         bridge/writeback.py bridge/dashboard.py bridge/config.py bridge/secrets_check.py

.PHONY: validate format test complexity setup_dev

validate:  ## lint + format-check + complexity + tests
	ruff check .
	ruff format --check .
	complexipy $(GATED) -mx 15
	pytest -q

format:  ## auto-fix lint + format
	ruff check . --fix
	ruff format .

test:  ## run the unit tests
	pytest -q

complexity:  ## cognitive complexity gate on the agent stack
	complexipy $(GATED) -mx 15

setup_dev:  ## install the pre-commit hooks
	uvx pre-commit install --hook-type pre-commit --hook-type commit-msg
