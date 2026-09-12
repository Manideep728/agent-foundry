# Convenience targets. Everything here is a plain uv command -- nothing in the
# project requires make. Targets are added as the behaviour behind them lands,
# so anything listed here works today.

PYTEST_ARGS ?=
PY_SRC      ?= control_plane tests

.PHONY: help sync test lint fmt typecheck check run clean

## help: list the documented targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## //'

## sync: create or update the virtualenv from pyproject.toml
sync:
	uv sync

## test: run the Python test suite
test:
	uv run pytest -q $(PYTEST_ARGS)

## lint: check style and common errors
lint:
	uv run ruff check .
	uv run ruff format --check .

## fmt: rewrite files to the project format
fmt:
	uv run ruff format .
	uv run ruff check --fix .

## typecheck: run mypy in strict mode
typecheck:
	uv run mypy $(PY_SRC)

## check: everything CI runs
check: lint typecheck test

## run: invoke the CLI (pass arguments with ARGS=...)
run:
	uv run af $(ARGS)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
