.PHONY: test test-python test-typescript test-php

test-python:
	uv run python run_python.py

test-typescript: build-typescript
	node run_typescript.mjs

test-php:
	php run_php.php

build-typescript:
	$(MAKE) -C ../../packages/payment-fee-typescript build

test: test-python test-typescript test-php
	@echo "All conformance suites passed."
