FROM rclone/rclone:1.74.4 AS rclone

FROM python:3.14-slim

LABEL org.opencontainers.image.title="FrostVault" \
      org.opencontainers.image.description="Self-hosted S3 archival and recovery" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PUID=99 \
    PGID=100
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
