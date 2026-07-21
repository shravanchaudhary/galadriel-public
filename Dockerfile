# syntax=docker/dockerfile:1
#
# Galadriel — ready-to-run container image.
#
# Two-stage build: the builder compiles wheels (incl. the heavier ChromaDB /
# onnxruntime stack that mempalace pulls in), the runtime stage stays slim.
#
#   docker build -t galadriel .
#   docker run --env-file .env -p 127.0.0.1:8080:8080 -v galadriel-data:/data galadriel
#
# Or just use docker-compose.yml (recommended): docker compose up -d --build
#
# Multi-arch: python:3.12-slim is published for amd64 and arm64, so a plain
# `docker build` works on both. For a registry push covering both:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <repo> --push .

# ---------- builder: compile wheels ----------
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------- runtime: slim final image ----------
FROM public.ecr.aws/docker/library/python:3.12-slim
LABEL org.opencontainers.image.title="Galadriel" \
      org.opencontainers.image.source="https://github.com/avasol/galadriel-public" \
      org.opencontainers.image.description="A persistent, self-hosted Claude agent with a verbatim memory palace."

# onnxruntime (transitive dep of mempalace) needs libgomp at runtime. iptables
# is used only by the ECS task's short-lived network init sidecar, never by the
# unprivileged application container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libgomp1 iptables \
    && curl --fail --silent --show-error \
        --output /etc/ssl/certs/rds-global-bundle.pem \
        https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Its home is /data so the palace defaults (~/.mempalace) land
# on the persistent volume with zero extra config.
RUN useradd --create-home --home-dir /data --uid 1000 galadriel
WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Application code. .dockerignore keeps keys/, .env, memory logs and bloat out.
COPY . .

# Keep immutable first-boot defaults separately from persisted runtime paths.
# The entrypoint copies only absent files, so upgrades never overwrite state.
RUN mkdir -p /opt/galadriel-defaults \
    && for dir in config knowledge memory state jobs workflows; do \
        mkdir -p "/app/$dir"; \
        cp -a "/app/$dir/." "/opt/galadriel-defaults/$dir/"; \
        rm -rf "/app/$dir"; \
        ln -s "/mnt/efs/$dir" "/app/$dir"; \
    done \
    && rm -rf /data \
    && ln -s /mnt/efs/data /data \
    && rm -rf /app/personal-tools \
    && ln -s /mnt/efs/personal-tools /app/personal-tools \
    && ln -s /etc/ssl/certs/rds-global-bundle.pem /app/global-bundle.pem \
    && chown -R galadriel:galadriel /app /opt/galadriel-defaults

# Palace lives under the user's home on the volume. These are the public
# defaults already (~/.mempalace), set explicitly here for clarity.
ENV MEMPALACE_PATH=/data/.mempalace/palace \
    PALACE_ARCHIVE_ROOT=/data/.mempalace/archive \
    PALACE_WAKE_UP_FILE=/data/.mempalace/wake_up.md \
    GALADRIEL_STORAGE_ROOT=/mnt/efs \
    GALADRIEL_ENFORCE_WRITE_BOUNDARIES=true \
    BROWSER_BACKEND=bce \
    TOWER_HOST=0.0.0.0 \
    TOWER_PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Tower form/session auth (TOWER_AUTH_*) protects the UI when enabled. Prefer
# binding to localhost or an authenticated edge; see docker-compose.yml.
EXPOSE 8080

USER galadriel
ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["python", "main.py"]
