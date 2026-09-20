# Convenience targets. Everything here is a plain uv, cmake or docker command --
# nothing in the project requires make. Targets are added as the behaviour behind them lands,
# so anything listed here works today.

PYTEST_ARGS ?=
PY_SRC      ?= control_plane tests

RUNNER_SRC        ?= runner
RUNNER_BUILD_TYPE ?= RelWithDebInfo
# Inside the tree, and ignored. A build directory shared between two checkouts
# fails the moment the second one configures, because CMake records the source
# path in its cache. Override it for a checkout on a slow filesystem.
RUNNER_BUILD_DIR  ?= $(RUNNER_SRC)/build
RUNNER_IMAGE      ?= afrunner:local
RUNNER_JOBS       ?= $(shell nproc 2>/dev/null || echo 2)

BENCH_ARGS  ?=
REPORT_ARGS ?=

.PHONY: help sync test lint fmt typecheck check run clean \
        runner-configure runner-build runner-test runner-image doctor bench report

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

## runner-configure: generate the build system for the C++ runner
runner-configure:
	cmake -S $(RUNNER_SRC) -B $(RUNNER_BUILD_DIR) -DCMAKE_BUILD_TYPE=$(RUNNER_BUILD_TYPE)

## runner-build: compile the C++ runner
runner-build: runner-configure
	cmake --build $(RUNNER_BUILD_DIR) -j

## runner-test: run the runner tests that need no container
runner-test: runner-build
	ctest --test-dir $(RUNNER_BUILD_DIR) -L native --output-on-failure

## runner-image: build the validation image, with afrunner as its entrypoint
runner-image:
	docker build --target runtime \
		--build-arg BUILD_TYPE=$(RUNNER_BUILD_TYPE) --build-arg JOBS=$(RUNNER_JOBS) \
		-t $(RUNNER_IMAGE) $(RUNNER_SRC)

## doctor: check that the container runtime enforces the validation limits
doctor:
	uv run af doctor --image $(RUNNER_IMAGE)

## bench: evaluate every task in tasks/ and summarise each suite
bench:
	uv run af bench --image $(RUNNER_IMAGE) $(BENCH_ARGS)

## report: write a Markdown report over the stored runs
report:
	uv run af report $(REPORT_ARGS)

## check: everything CI runs
check: lint typecheck test

## run: invoke the CLI (pass arguments with ARGS=...)
run:
	uv run af $(ARGS)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
