# Stage 1: Builder — installs runtime dependencies only; no test tooling.
# Digest pinned to python:3.12.9-slim. Refresh on dependency-bump rotations:
#   docker pull python:3.12.9-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12.9-slim
FROM python:3.12.9-slim@sha256:48a11b7ba705fd53bf15248d1f94d36c39549903c5d59edcfa2f3f84126e7b44 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# No build toolchain is installed: every pinned dependency in requirements.lock
# ships a prebuilt cp312 wheel for both linux/amd64 and linux/arm64, and the
# project itself is pure Python, so nothing is ever compiled from an sdist and
# gcc is never invoked. This shaves the apt layer (and its slow dpkg unpack)
# off both arch builds. If a future dependency lacks a wheel for a target arch,
# the build fails loudly at the pip step below — reinstate build-essential
# (plus rust, for pyo3/maturin packages such as cryptography or pydantic-core)
# at that point.
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy README.md so `pip install .` can read the readme field in pyproject.toml.
COPY README.md ./
COPY pyproject.toml ./
COPY requirements.lock ./
COPY src/ src/
# Pin all transitive dependencies to the audited versions in requirements.lock,
# then install the project itself (which re-uses the already-installed deps),
# then pre-compile the whole venv to .pyc so the runtime image starts with
# bytecode already present. compileall writes bytecode regardless of
# PYTHONDONTWRITEBYTECODE, and COPYing the venv preserves the .py mtimes so the
# .pyc files stay valid in the final stage (both stages pin the same digest, so
# the interpreter that compiled them matches the one that loads them).
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && python -m compileall -q /opt/venv

# Stage 2: Production — lean image; no build tools, no test deps.
# Same digest as the builder stage — both must match for reproducible builds.
FROM python:3.12.9-slim@sha256:48a11b7ba705fd53bf15248d1f94d36c39549903c5d59edcfa2f3f84126e7b44

# PYTHONDONTWRITEBYTECODE is deliberately NOT set in the runtime stage (only in
# the builder). The venv copied from the builder already ships pre-compiled
# .pyc, so imports are fast; leaving bytecode writing enabled lets Python cache
# any lazily-imported module on first use instead of recompiling it every time.
ENV PYTHONUNBUFFERED=1

# Create a fixed UID/GID 1000:1000 so the container always owns /data
# with a predictable numeric identity. The compose file does not need to
# override `user:` — it is documented but redundant now and has been removed.
RUN groupadd --gid 1000 mediaman \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin mediaman
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /data && chown mediaman:mediaman /data
VOLUME /data

# Run as non-root uid/gid 1000:1000 (mediaman user created above).
USER mediaman
EXPOSE 8282

# Simple liveness check. /healthz returns 200 when the app is up.
# Reads MEDIAMAN_PORT at probe time so a custom port flows through; falls
# back to 8282 when unset (matches the EXPOSE / config defaults).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('MEDIAMAN_PORT', '8282') + '/healthz')" \
        || exit 1

CMD ["mediaman"]
