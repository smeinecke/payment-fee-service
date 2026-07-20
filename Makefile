# Makefile for payment-fee-service workspace

DOCKER_IMAGE ?= ghcr.io/smeinecke/payment-fee-service
COMPOSER_BIN ?= $(shell command -v composer 2>/dev/null || echo /tmp/composer)

.PHONY: all format check validate test test-python test-php test-typescript test-conformance test-isolated test-unit test-e2e test-live build audit-contract docker-build docker-smoke docker-push lock-check help paypal-sandbox-validate-config paypal-sandbox-probe paypal-sandbox-probe-nvp paypal-sandbox-plan paypal-sandbox-smoke paypal-sandbox-report paypal-sandbox-install-playwright paypal-sandbox-manual-plan paypal-sandbox-manual-smoke paypal-sandbox-manual-formula paypal-sandbox-manual-report

all: validate test-unit

lock-check:
	uv lock --check

format:
	uv run ruff format --check --diff .

reformat-ruff:
	uv run ruff format .

check:
	uv run ruff check .

fix-ruff:
	uv run ruff check . --fix

fix: reformat-ruff fix-ruff
	@echo "Updated code."

pyright:
	uv run pyright

bandit:
	uv run bandit -r packages services -ll

test:
	uv run pytest tests/

test-unit:
	uv run pytest tests/ -m "not live and not e2e"

test-e2e:
	uv run pytest tests/e2e -m e2e

test-live:
	uv run pytest tests/ -m live

test-python:
	uv run pytest tests/ -m "not live and not e2e"

test-php:
	$(MAKE) -C packages/payment-fee-php test COMPOSER_BIN=$(COMPOSER_BIN)

test-typescript:
	$(MAKE) -C packages/payment-fee-typescript test

test-conformance:
	$(MAKE) -C tools/conformance test

test-isolated:
	COMPOSER_BIN=$(COMPOSER_BIN) tools/isolated-install/test_php.sh
	tools/isolated-install/test_typescript.sh

audit-contract:
	PAYPAL_FEE_DATA=$(PAYPAL_FEE_DATA) STRIPE_FEE_DATA=$(STRIPE_FEE_DATA) uv run python tools/audit_contract_runner.py

PAYPAL_FEE_DATA ?= paypal-fee-data
STRIPE_FEE_DATA ?= stripe-fee-data

build: docker-build

docker-build:
	docker buildx build --load --platform linux/amd64 -t $(DOCKER_IMAGE):local .

docker-smoke: docker-build
	scripts/docker-smoke.sh $(DOCKER_IMAGE):local

docker-push:
	docker buildx build --push --platform linux/amd64,linux/arm64 -t $(DOCKER_IMAGE):latest .

validate: format check pyright bandit
	@echo "Python validation passed."

# -----------------------------------------------------------------------------
# PayPal Sandbox validation harness targets
# -----------------------------------------------------------------------------

PAYPAL_SANDBOX_ACCOUNTS_CSV ?= $(HOME)/paypal-sandbox-accounts.csv
PAYPAL_SANDBOX_VALIDATION_RUN ?= $(shell ls -t artifacts/paypal-sandbox 2>/dev/null | head -1)

paypal-sandbox-validate-config:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation validate-config

paypal-sandbox-probe:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation probe

paypal-sandbox-probe-nvp:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation probe-nvp

paypal-sandbox-plan:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation plan --profile smoke

paypal-sandbox-smoke:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation run --profile smoke

paypal-sandbox-smoke-continue:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation run --profile smoke --continue-after-mismatch

paypal-sandbox-de-compliance:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation run --profile de-compliance-probe --merchant DE --buyer DE

paypal-sandbox-report:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation report --run-id $(PAYPAL_SANDBOX_VALIDATION_RUN)

paypal-sandbox-install-playwright:
	uv run python -m playwright install chromium

paypal-sandbox-manual-plan:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation manual-plan --profile manual-de-first

paypal-sandbox-manual-smoke:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation manual-plan --profile manual-de-first && \
		PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation manual-run --run-id $$(ls -t artifacts/paypal-sandbox-manual 2>/dev/null | head -1)

paypal-sandbox-manual-formula:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation manual-plan --profile manual-de-formula && \
		PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation manual-run --run-id $$(ls -t artifacts/paypal-sandbox-manual 2>/dev/null | head -1)

paypal-sandbox-manual-report:
	PAYPAL_SANDBOX_ACCOUNTS_CSV=$(PAYPAL_SANDBOX_ACCOUNTS_CSV) \
		uv run paypal-sandbox-validation manual-report --run-id $$(ls -t artifacts/paypal-sandbox-manual 2>/dev/null | head -1)

help:
	@echo "Available targets:"
	@echo "  all             - Run validation and unit tests (default)"
	@echo "  format          - Check code formatting with ruff"
	@echo "  reformat-ruff   - Format code with ruff"
	@echo "  check           - Run ruff linting"
	@echo "  fix-ruff        - Auto-fix ruff issues"
	@echo "  fix             - Run reformat-ruff and fix-ruff"
	@echo "  pyright         - Run Python type checking"
	@echo "  bandit          - Run security analysis"
	@echo "  test            - Run all tests"
	@echo "  test-python     - Run Python tests"
	@echo "  test-php        - Run PHP tests"
	@echo "  test-typescript - Run TypeScript tests"
	@echo "  test-conformance- Run cross-language conformance tests"
	@echo "  test-isolated   - Run isolated package installation tests for PHP and TypeScript"
	@echo "  test-unit       - Run Python unit tests (no live network, no e2e)"
	@echo "  test-e2e        - Run end-to-end tests against a real server"
	@echo "  test-live       - Run live integration tests"
	@echo "  audit-contract  - Run contract audit across languages"
	@echo "  build           - Build all package artifacts"
	@echo "  docker-build    - Build a local Docker image for linux/amd64"
	@echo "  docker-push     - Build and push the Docker image to GHCR for linux/amd64+arm64"
	@echo "  paypal-sandbox-validate-config     - Validate PayPal Sandbox account configuration"
	@echo "  paypal-sandbox-probe               - Probe OAuth credentials for all merchants"
	@echo "  paypal-sandbox-probe-nvp           - Probe NVP credentials for all merchants"
	@echo "  paypal-sandbox-manual-plan         - Create a manual Send Money validation plan"
	@echo "  paypal-sandbox-manual-smoke        - Run the DE manual Send Money first case"
	@echo "  paypal-sandbox-manual-formula      - Run the DE manual Send Money formula suite"
	@echo "  paypal-sandbox-manual-report       - Report the latest manual validation run"
	@echo "  validate        - Run all validation checks"
	@echo "  help            - Show this help message"
