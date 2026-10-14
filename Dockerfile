# syntax=docker/dockerfile:1.7

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_BASE_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install ".[api]"


FROM builder AS test

COPY tests ./tests

RUN python -m pip install ".[api,test]"
RUN python -m pytest


FROM ${PYTHON_BASE_IMAGE} AS runtime-base

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NTP_ANCHOR_API_HOST=127.0.0.1 \
    NTP_ANCHOR_API_PORT=8000 \
    NTP_ANCHOR_LOG_FILE=/var/log/ntp-anchor/ntp-anchor.log

RUN apt-get update \
    && apt-get install --yes --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN mkdir -p /var/log/ntp-anchor \
    && chown 65532:0 /var/log/ntp-anchor \
    && chmod 0775 /var/log/ntp-anchor

WORKDIR /app

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json, os, urllib.request; port = os.environ.get('NTP_ANCHOR_API_PORT', '8000'); response = urllib.request.urlopen(f'http://127.0.0.1:{port}/ping', timeout=2); raise SystemExit(0 if json.load(response) == {'status': 'pong'} else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "ntp-anchor-api"]


# Explicit Linux host-clock correction target. The Compose override grants only
# CAP_SYS_TIME at runtime; never run this target with Docker's privileged mode.
FROM runtime-base AS correction

USER 0:0

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ntpsec-ntpdate \
    && rm -rf /var/lib/apt/lists/*


# Safe default target: measurement API only, without root or a correction tool.
FROM runtime-base AS runtime

USER 65532:0
