# ProviderMap - runnable container.
#
# Build:  docker build -t providermap .
# Run:    docker compose run --rm providermap test
#         (see docker-compose.yml; running the image directly needs the
#          volume mounts spelled out by hand)
#
# This container runs the pipeline; it does not need a database server or a
# browser (Playwright/Selenium) at any point - see the README for why.

FROM python:3.12-slim AS base

# Fail fast and don't buffer output - this is a batch job, not a server.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so `docker build` only reinstalls them when
# requirements.txt or pyproject.toml actually change, not on every source edit.
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY providermap/ ./providermap/
COPY adapters/ ./adapters/
COPY run.py config.example.yaml ./

# A non-root user - this container makes outbound HTTP requests against a
# third party; it has no reason to run as root.
RUN useradd --create-home --uid 1000 providermap \
    && mkdir -p /app/database /app/logs /app/exports/output /app/.cache \
    && chown -R providermap:providermap /app
USER providermap

# config.yaml, database/, logs/, and exports/ are all expected to be bind- or
# volume-mounted at runtime - see docker-compose.yml. The container has no
# config.yaml of its own beyond the example, since it needs a real contact
# email before it can run against a live site.
VOLUME ["/app/database", "/app/logs", "/app/exports/output"]

ENTRYPOINT ["python", "run.py"]
CMD ["--help"]
