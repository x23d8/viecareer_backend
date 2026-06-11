# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# System deps needed to compile wheels (psycopg2-binary ships pre-built,
# but sounddevice needs PortAudio headers at install time on some platforms).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libportaudio2 \
        portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements first so this layer is cached unless deps change.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Runtime stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime-only system lib required by sounddevice.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security.
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /api

# Copy installed packages from the builder stage.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source (after deps so code changes don't bust the dep layer).
COPY --chown=appuser:appgroup . .

USER appuser

# PORT defaults to 8000; override at runtime (e.g. Cloud Run sets PORT automatically).
ENV PORT=8000

EXPOSE ${PORT}

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]