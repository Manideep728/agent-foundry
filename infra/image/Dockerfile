# The control plane as an image: `af worker`, `af collect` and `af dispatch`
# for a machine that has Docker and nothing else.
#
# Built by `make control-plane-image` from `git archive HEAD`, so the context
# is a commit: no local database, no run artifacts, no untracked file reaches
# the image, and the version baked in names exactly what is inside.
#
# A worker in this image does not run validation itself. It starts the runner
# image as a sibling container through the host's Docker socket (D1: the
# supervisor is PID 1 of its own container), so the host must already hold
# that image, and the worker's runs directory must be mounted at the same path
# it has on the host -- the daemon resolves bind mounts on the host.
FROM python:3.12-slim-bookworm

ARG DOCKER_VERSION=29.1.3
ARG TARGETARCH=amd64

# The Docker CLI only, as the static binary: the daemon is the host's.
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64 ;; \
      arm64) arch=aarch64 ;; \
      *) echo "unsupported architecture $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    python - "$DOCKER_VERSION" "$arch" <<'PY'
import io
import sys
import tarfile
import urllib.request

version, arch = sys.argv[1], sys.argv[2]
url = f"https://download.docker.com/linux/static/stable/{arch}/docker-{version}.tgz"
with urllib.request.urlopen(url, timeout=60) as response:
    archive = tarfile.open(fileobj=io.BytesIO(response.read()), mode="r:gz")
member = archive.getmember("docker/docker")
member.name = "docker"
archive.extract(member, "/usr/local/bin", filter="data")
PY

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --locked --no-dev --no-install-project \
    --extra kafka --extra postgres

COPY control_plane control_plane
COPY tasks tasks
RUN uv sync --locked --no-dev --extra kafka --extra postgres

ARG AF_HARNESS_VERSION
ENV AF_HARNESS_VERSION=${AF_HARNESS_VERSION} \
    PATH=/app/.venv/bin:$PATH

# Not root. The worker passes its own uid and gid to `docker run --user`, so
# the runner and the files it writes into the shared runs directory get this
# identity too. Socket access comes from the group the socket has on the host,
# added at `docker run` time with `--group-add`.
RUN useradd --uid 1000 --create-home af
USER af

ENTRYPOINT ["af"]
