# Makefile for paypal-fee-crawler

DOCKER_IMAGE ?= ghcr.io/smeinecke/payment-fee-service

.PHONY: all format check validate test test-unit test-e2e test-live docker-build docker-push help

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
	uv run bandit -c pyproject.toml -r src

test:
	uv run pytest

test-unit:
	uv run pytest tests/ -m "not live and not e2e"

test-e2e:
	uv run pytest tests/e2e -m e2e

test-live:
	uv run pytest tests/ -m live

docker-build:
	docker buildx build --load --platform linux/amd64 -t $(DOCKER_IMAGE):local .

docker-push:
	docker buildx build --push --platform linux/amd64,linux/arm64 -t $(DOCKER_IMAGE):latest .

validate: format check pyright bandit
	@echo "Validation passed."

help:
	@echo "Available targets:"
	@echo "  all           - Run validation and unit tests (default)"
	@echo "  format        - Check code formatting with ruff"
	@echo "  reformat-ruff - Format code with ruff"
	@echo "  check         - Run ruff linting"
	@echo "  fix-ruff      - Auto-fix ruff issues"
	@echo "  fix           - Run reformat-ruff and fix-ruff"
	@echo "  pyright       - Run type checking"
	@echo "  bandit        - Run security analysis"
	@echo "  test          - Run all tests"
	@echo "  test-unit     - Run unit tests (no live network, no e2e)"
	@echo "  test-e2e      - Run end-to-end tests against a real server"
	@echo "  test-live     - Run live integration tests"
	@echo "  docker-build  - Build a local Docker image for linux/amd64"
	@echo "  docker-push   - Build and push the Docker image to GHCR for linux/amd64+arm64"
	@echo "  validate      - Run all validation checks"
	@echo "  help          - Show this help message"
