.PHONY: validate format test coverage typecheck complexity setup setup_dev setup_prod

validate:  ## lint + typecheck + format-check + tests
	uv run --locked ruff check .
	uv run --locked pyright bridge/
	uv run --locked ruff format --check .
	uv run --locked pytest -q --cov=bridge --cov-report=term-missing --cov-fail-under=80
	uv run --locked complexipy bridge/ -mx 15

format:  ## auto-fix lint + format
	uv run --locked ruff check . --fix
	uv run --locked ruff format .

test:  ## run the unit tests
	uv run --locked pytest -q

coverage:  ## run tests with the bridge coverage gate
	uv run --locked pytest -q --cov=bridge --cov-report=term-missing --cov-fail-under=80

typecheck:  ## static type check (pyright, bridge/ only — vm-agent is Windows/comtypes)
	uv run --locked pyright bridge/

complexity:  ## enforce cognitive complexity <=15 for bridge functions
	uv run --locked complexipy bridge/ -mx 15

setup:  ## create/update the locked project environment
	uv sync --locked

setup_dev: setup  ## create the environment and install pre-commit hooks
	uv run --locked pre-commit install --hook-type pre-commit --hook-type commit-msg

setup_prod:  ## install only locked bridge runtime dependencies
	uv sync --locked --no-default-groups
