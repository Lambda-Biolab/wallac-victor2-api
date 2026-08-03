# Makefile — repeatable development and quality gates for the maintained stacks.

.PHONY: validate format test coverage typecheck complexity secrets bandit mutate mutate-stats install_tools \
	validate-branch pre-push-validate setup setup_dev setup_prod clean

UV := uv
SOURCE_DIR := bridge
TEST_DIR := tests
COMPLEXITY_MAX ?= 15

validate: lint typecheck complexity coverage secrets bandit  ## Run the full CI quality gate (secrets skipped in CI: gitleaks has no binary; the .github/workflows/secrets.yml job covers history)

lint:  ## Run Ruff lint and format checks
	$(UV) run --locked ruff check $(SOURCE_DIR) $(TEST_DIR)
	$(UV) run --locked ruff format --check $(SOURCE_DIR) $(TEST_DIR)

format:  ## Auto-fix Ruff lint and formatting
	$(UV) run --locked ruff check $(SOURCE_DIR) $(TEST_DIR) --fix
	$(UV) run --locked ruff format $(SOURCE_DIR) $(TEST_DIR)

test:  ## Run the unit test suite
	$(UV) run --locked pytest -q

coverage:  ## Run tests with the bridge coverage gate
	$(UV) run --locked pytest -q --cov=$(SOURCE_DIR) --cov-report=term-missing --cov-fail-under=80

typecheck:  ## Type-check bridge and tests
	$(UV) run --locked pyright $(SOURCE_DIR)/ $(TEST_DIR)/

complexity:  ## Enforce cognitive complexity <=15
	$(UV) run --locked complexipy $(SOURCE_DIR)/ -mx $(COMPLEXITY_MAX)

secrets:  ## Scan the working tree for secrets with gitleaks (skipped in CI: see .github/workflows/secrets.yml)
	@if [ -n "$$CI" ] && [ ! -x "$$(command -v gitleaks 2>/dev/null || true)" ]; then \
		echo "secrets: skipping in CI (gitleaks binary not installed; history scan runs in .github/workflows/secrets.yml)"; \
	elif ! command -v gitleaks >/dev/null 2>&1; then \
		echo "gitleaks not installed — run 'make install_tools'"; \
		exit 2; \
	else \
		gitleaks detect --no-git --source . --config .github/gitleaks.toml --redact; \
	fi

bandit:  ## Run Python SAST on bridge
	$(UV) run --locked bandit -r $(SOURCE_DIR) -ll -ii

mutate:  ## Run mutation tests locally (not part of CI)
	@find bridge tests -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	$(UV) run --locked mutmut run

mutate-stats:  ## Show mutation-testing results
	$(UV) run --locked mutmut results

install_tools:  ## Install the pinned gitleaks binary
	@echo "Installing gitleaks 8.30.1..."
	@if ! command -v gitleaks >/dev/null 2>&1; then \
		mkdir -p $(HOME)/.local/bin; \
		curl -sL https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
			| tar -xz -C $(HOME)/.local/bin gitleaks; \
		chmod +x $(HOME)/.local/bin/gitleaks; \
		echo "installed to $(HOME)/.local/bin/gitleaks"; \
	else \
		echo "gitleaks already present at $$(command -v gitleaks)"; \
	fi

validate-branch:  ## Enforce branch coverage on changed bridge files
	@CHANGED=$$(git diff --name-only --diff-filter=ACMR origin/main 2>/dev/null | grep -E '^$(SOURCE_DIR)/.*\.py$$' || true); \
	if [ -n "$$CHANGED" ]; then \
		echo "Changed bridge files:"; echo "$$CHANGED"; \
		$(UV) run --locked pytest tests/ -q --cov=$(SOURCE_DIR) --cov-branch --cov-fail-under=80; \
	else \
		echo "No changed bridge files; skipping branch coverage check"; \
	fi

pre-push-validate: validate validate-branch  ## Push gate: full CI-mirrored quality + branch coverage. Mutation testing is a separate manual gate (see `make mutate`).

setup:  ## Create/update the locked project environment
	$(UV) sync --locked

setup_dev: setup  ## Install pre-commit hooks
	$(UV) run --locked pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push

setup_prod:  ## Install only locked runtime dependencies
	$(UV) sync --locked --no-default-groups

clean:  ## Remove generated caches and reports
	rm -rf .pytest_cache .ruff_cache .complexipy_cache .coverage htmlcov .pyright
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
