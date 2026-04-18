# Stage 1: Builder + tester
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY src/ src/
COPY tests/ tests/
RUN pip install --no-cache-dir -e .
RUN pytest -q --tb=short

# Stage 2: Production
FROM python:3.11-slim

RUN groupadd -r mediaman && useradd -r -g mediaman mediaman
WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

RUN mkdir -p /data && chown mediaman:mediaman /data
VOLUME /data

USER mediaman
EXPOSE 8282

CMD ["mediaman"]
