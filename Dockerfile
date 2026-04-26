# Stage 1: Builder — installs runtime dependencies only; no test tooling.
# Pin to a specific minor release so builds are reproducible.
# TODO(P7): pin to a digest (python:3.12.x-slim@sha256:...) and automate
#           rotation via Renovate/Dependabot once CI is in place.
FROM python:3.12.9-slim AS builder

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
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .

# Stage 2: Production — lean image; no build tools, no test deps.
FROM python:3.12.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd -r mediaman && useradd -r -g mediaman mediaman
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /data && chown mediaman:mediaman /data
VOLUME /data

# Run as non-root. The host uid/gid can be overridden at runtime with
# `docker run --user 1000:1000` or via the compose `user:` key.
USER mediaman
EXPOSE 8282

# Simple liveness check. /healthz returns 200 when the app is up.
# TODO(P3): add /healthz route; replace `curl` with a native Python probe
#           if curl is not available in the slim image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8282/healthz')" \
        || exit 1

CMD ["mediaman"]
