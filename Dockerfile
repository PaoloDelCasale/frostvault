FROM node:26-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build:ci

# Keep the runtime on the Python 3.12 line exercised by every Python CI job.
# Pin the official multi-platform index; Dependabot maintains tag + digest.
FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58

LABEL org.opencontainers.image.title="FrostVault" \
      org.opencontainers.image.description="Self-hosted S3 archival and recovery" \
      org.opencontainers.image.licenses="Apache-2.0"

# postgresql-client-16: pg_dump/pg_restore/createdb/dropdb/psql for metadata backups
# against the supported PostgreSQL 16 server (issue #7). PGDG pins major 16 even when
# the base image's Debian default client is a different major.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gosu \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && printf '%s\n' \
        "Types: deb" \
        "URIs: https://apt.postgresql.org/pub/repos/apt" \
        "Suites: ${VERSION_CODENAME}-pgdg" \
        "Components: main" \
        "Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc" \
        > /etc/apt/sources.list.d/pgdg.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# The Compose templates run without CAP_SETUID/CAP_SETGID and start directly
# as numeric PUID:PGID, so the documented Unraid default must exist before the
# read-only runtime starts. Overrides need no account and never mutate /etc.
RUN set -eux; \
    if getent passwd archive >/dev/null; then \
        echo "archive user is unexpectedly already present in the base image" >&2; \
        exit 1; \
    fi; \
    if getent passwd 99 >/dev/null; then \
        echo "default PUID 99 is unexpectedly already allocated in the base image" >&2; \
        exit 1; \
    fi; \
    if ! getent group 100 >/dev/null; then \
        groupadd --gid 100 archive; \
    fi; \
    useradd --system --uid 99 --gid 100 \
        --home-dir /app --shell /usr/sbin/nologin --no-create-home archive

WORKDIR /app
COPY requirements.txt .
# Pip is only needed while assembling the image; removing it keeps its vendored
# package copies out of the runtime vulnerability scan.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall --yes pip

COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations
COPY --from=frontend /frontend/dist ./frontend/dist
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PUID=99 \
    PGID=100 \
    FRONTEND_DIST_DIR=/app/frontend/dist
EXPOSE 8080

# Inherited by both Compose templates. Send an authorized Host to the real
# dependency/worker readiness route; never relax Host validation for loopback.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "app.readiness_probe"]

ENTRYPOINT ["/entrypoint.sh"]
# Notify SSE/workers before uvicorn drains HTTP (5s); workers drain for 20s.
# The process-wide 30s failsafe also covers non-cooperative executor joins.
CMD ["python", "-m", "app.server", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
