# Makefile for payment-fee-service workspace

DOCKER_IMAGE ?= ghcr.io/smeinecke/payment-fee-service

.PHONY: all format check validate test test-python test-php test-typescript test-conformance test-unit test-e2e test-live build audit-contract docker-build docker-push help

all: validate test-unit

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
	$(MAKE) -C packages/payment-fee-php test

test-typescript:
	$(MAKE) -C packages/payment-fee-typescript test

test-conformance:
	$(MAKE) -C tools/conformance test

audit-contract:
	uv run python tools/audit_contract_runner.py

build: docker-build

docker-build:
	docker buildx build --load --platform linux/amd64 -t $(DOCKER_IMAGE):local .

docker-push:
	docker buildx build --push --platform linux/amd64,linux/arm64 -t $(DOCKER_IMAGE):latest .

validate: format check pyright bandit
	@echo "Python validation passed."

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
	@echo "  test-unit       - Run Python unit tests (no live network, no e2e)"
	@echo "  test-e2e        - Run end-to-end tests against a real server"
	@echo "  test-live       - Run live integration tests"
	@echo "  audit-contract  - Run contract audit across languages"
	@echo "  build           - Build all package artifacts"
	@echo "  docker-build    - Build a local Docker image for linux/amd64"
	@echo "  docker-push     - Build and push the Docker image to GHCR for linux/amd64+arm64"
	@echo "  validate        - Run all validation checks"
	@echo "  help            - Show this help message"
