# Stage 1: Builder — installs runtime dependencies only; no test tooling.
# Digest pinned to python:3.12.9-slim. Refresh on dependency-bump rotations:
#   docker pull python:3.12.9-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12.9-slim
FROM python:3.12.9-slim@sha256:48a11b7ba705fd53bf15248d1f94d36c39549903c5d59edcfa2f3f84126e7b44 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy README.md so `pip install .` can read the readme field in pyproject.toml.
COPY README.md ./
COPY pyproject.toml ./
COPY requirements.lock ./
COPY src/ src/
# Pin all transitive dependencies to the audited versions in requirements.lock,
# then install the project itself (which re-uses the already-installed deps).
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip install --no-cache-dir --no-deps .

# Stage 2: Production — lean image; no build tools, no test deps.
# Same digest as the builder stage — both must match for reproducible builds.
FROM python:3.12.9-slim@sha256:48a11b7ba705fd53bf15248d1f94d36c39549903c5d59edcfa2f3f84126e7b44

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create a fixed UID/GID 1000:1000 so the container always owns /data
# with a predictable numeric identity. The compose file does not need to
# override `user:` — it is documented but redundant now and has been removed.
RUN groupadd --gid 1000 mediaman && useradd --uid 1000 --gid 1000 --no-create-home mediaman
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
