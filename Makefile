.PHONY: validate format test typecheck complexity setup setup_dev setup_prod

validate:  ## lint + typecheck + format-check + tests
	uv run --locked ruff check .
	uv run --locked pyright bridge/
	uv run --locked ruff format --check .
	uv run --locked pytest -q

format:  ## auto-fix lint + format
	uv run --locked ruff check . --fix
	uv run --locked ruff format .

test:  ## run the unit tests
	uv run --locked pytest -q

typecheck:  ## static type check (pyright, bridge/ only — vm-agent is Windows/comtypes)
	uv run --locked pyright bridge/

complexity:  ## cognitive complexity (informational only — not gated)
	@echo "Complexity check is informational only. Run: uv run --locked complexipy bridge/ -mx 15"

setup:  ## create/update the locked project environment
	uv sync --locked

setup_dev: setup  ## create the environment and install pre-commit hooks
	uv run --locked pre-commit install --hook-type pre-commit --hook-type commit-msg

setup_prod:  ## install only locked bridge runtime dependencies
	uv sync --locked --no-default-groups
