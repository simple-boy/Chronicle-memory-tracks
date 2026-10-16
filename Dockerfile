ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}

ARG SOURCE_COMMIT=unfixed
ARG FORGE_PIP_INDEX=https://pypi.org/simple
LABEL org.opencontainers.image.title="ForgeTrace" \
      org.opencontainers.image.description="Source-preserving noise-resistant software engineering memory" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    MEMORY_DB_PATH=/app/data/forgetrace.sqlite3 \
    PRODUCTION=1 \
    SOURCE_COMMIT=${SOURCE_COMMIT} \
    HOME=/tmp \
    TMPDIR=/tmp
WORKDIR /app
COPY requirements.txt requirements.lock /app/
RUN pip install --no-cache-dir --index-url "${FORGE_PIP_INDEX}" -r /app/requirements.lock \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data
COPY src /app/src
COPY scripts/deploy.sh /app/scripts/deploy.sh
RUN chmod 0555 /app/scripts/deploy.sh
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"

# Initializes only the dedicated mount, then execs Python as UID/GID 10001.
ENTRYPOINT ["/bin/sh", "/app/scripts/deploy.sh", "--container-start"]
CMD ["python", "-m", "uvicorn", "forgetrace.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--log-level", "info", "--timeout-graceful-shutdown", "1800"]
