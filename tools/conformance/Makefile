.PHONY: test test-python

test-python:
	uv run python tools/conformance/run_python.py

test: test-python
	@echo "Cross-language conformance runner: Python OK. PHP and TypeScript runners are not yet implemented."
