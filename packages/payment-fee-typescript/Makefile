.PHONY: install test typecheck lint format-check format build pack audit

install:
	npm ci

test: build
	npm test

typecheck:
	npm run typecheck

lint:
	npm run lint

format-check:
	npm run format:check

format:
	npm run format

build:
	npm run build

pack: build
	npm pack --dry-run

audit:
	npm audit
