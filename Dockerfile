# Dockerfile — radio-ripper-tag (fingerprint, enrich, tag)
# Build:   docker build -t radio-ripper-tag:latest .
# Run:     docker run --rm --name ripper-tag \
#            -v "$PWD/work:/app/work" \
#            -v "$PWD/recordings:/app/recordings" \
#            -v "$PWD/mp3_inbox:/app/mp3_inbox" \
#            radio-ripper-tag:latest

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml LICENSE README.md uv.lock* ./
RUN uv sync --no-install-project --quiet

COPY src/ src/
RUN uv sync --quiet

FROM python:3.12-slim

LABEL org.opencontainers.image.title="radio-ripper-tag" \
      org.opencontainers.image.version="2.1.0" \
      org.opencontainers.image.description="Webradio tagger — AcoustID fingerprinting, iTunes enrichment, ID3v2 tagging" \
      org.opencontainers.image.source="https://github.com/domoskanonos/radioripper"

RUN groupadd --system --gid 1001 ripper \
 && useradd --system --uid 1001 --gid ripper --home-dir /app --shell /usr/sbin/nologin ripper

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/LICENSE /app/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACOUSTID_API_URL="https://api.acoustid.org/v2/lookup"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/recordings /app/work /app/mp3_inbox \
 && chown -R ripper:ripper /app

USER ripper

ENTRYPOINT ["radio-ripper"]
