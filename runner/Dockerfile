# The validation image. afrunner is the entrypoint, so it runs as PID 1 inside
# the container (D1), which is the only place its orphan reaping and signal
# handling are real.

FROM debian:bookworm AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ cmake make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt ./
COPY src ./src

# The same build type as the native build. JOBS stays low by default because a
# wide parallel build can exhaust a small Docker VM, such as the Mac's 1.9 GiB.
ARG BUILD_TYPE=RelWithDebInfo
ARG JOBS=2

# Tests off: the image holds only the runner, and the test suite would fetch
# GoogleTest from the network on every uncached build.
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE="$BUILD_TYPE" -DAFRUNNER_BUILD_TESTS=OFF \
    && cmake --build /build -j "$JOBS" --target afrunner

# The runtime carries the task toolchain as well as the runner: the synthetic
# tasks validate with `python3 -m unittest`. It stays on bookworm so the binary
# finds the same libstdc++ it was linked against.
FROM python:3.12-slim-bookworm AS runtime

COPY --from=build /build/afrunner /usr/local/bin/afrunner

# Exec form, so the flags given to `docker run` arrive as afrunner's argv. The
# shell form wraps the entrypoint in `/bin/sh -c` and silently drops them.
ENTRYPOINT ["/usr/local/bin/afrunner"]
