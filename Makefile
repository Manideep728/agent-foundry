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
CONTROL_IMAGE     ?= af-control-plane:local

BENCH_ARGS  ?=
REPORT_ARGS ?=

.PHONY: help sync test lint fmt typecheck check run clean \
        runner-configure runner-build runner-test runner-image doctor bench report \
        kafka-up kafka-down fanout-check pg-up pg-down control-plane-image infra-check

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

## control-plane-image: build the af image from the committed tree (refuses a dirty one)
control-plane-image:
	@git diff --quiet HEAD -- || { 		echo "control-plane-image: commit first; the image is built from HEAD" >&2; 		exit 1; }
	git archive --format=tar HEAD | docker build -f infra/image/Dockerfile 		--build-arg AF_HARNESS_VERSION=$$(uv run python -c 			'from control_plane.adapters.system import harness_version; print(harness_version())') 		-t $(CONTROL_IMAGE) -

## doctor: check that the container runtime enforces the validation limits
doctor:
	uv run af doctor --image $(RUNNER_IMAGE)

## bench: evaluate every task in tasks/ and summarise each suite
bench:
	uv run af bench --image $(RUNNER_IMAGE) $(BENCH_ARGS)

## report: write a Markdown report over the stored runs
report:
	uv run af report $(REPORT_ARGS)

## kafka-up: start the development broker and create the topics
kafka-up:
	sh infra/kafka/broker.sh
	sh infra/kafka/topics.sh

## kafka-down: stop the development broker; its topics are kept
kafka-down:
	docker stop af-kafka

## pg-up: start the development PostgreSQL
pg-up:
	sh infra/postgres/server.sh

## pg-down: stop the development PostgreSQL; its data is kept
pg-down:
	docker stop af-postgres

## fanout-check: measure the suite over Kafka and compare it with af bench
fanout-check:
	AF_IMAGE=$(RUNNER_IMAGE) sh infra/kafka/fanout-check.sh

## infra-check: format-check and validate the AWS Terraform, without credentials
infra-check:
	terraform -chdir=infra/aws fmt -check -recursive
	terraform -chdir=infra/aws init -backend=false -input=false >/dev/null
	terraform -chdir=infra/aws validate

## check: everything CI runs
check: lint typecheck test

## run: invoke the CLI (pass arguments with ARGS=...)
run:
	uv run af $(ARGS)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
