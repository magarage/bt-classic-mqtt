# ---- build stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy source and install
COPY pyproject.toml .
COPY src/ src/
COPY devices/ devices/
RUN uv pip install --system --no-cache .


# ---- runtime stage ----
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/bt-classic-mqtt /usr/local/bin/bt-classic-mqtt
COPY src/ src/
COPY devices/ devices/

ENV PYTHONUNBUFFERED=1

CMD ["bt-classic-mqtt"]
