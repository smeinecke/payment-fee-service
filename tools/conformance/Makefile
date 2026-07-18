.PHONY: test test-python test-typescript test-php test-differential build-typescript

test-python:
	uv run python run_python.py

test-typescript: build-typescript
	node run_typescript.mjs

test-php:
	php run_php.php

test-differential: build-typescript
	uv run python run_differential.py

build-typescript:
	$(MAKE) -C ../../packages/payment-fee-typescript build

test: test-python test-typescript test-php test-differential
	@echo "All conformance suites passed."
