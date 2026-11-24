# One image serves the web app, the Outlook worker and the migration job; the
# command chosen in docker-compose.yml decides which role a container plays.
# Build from the repository root: docker build -f docker/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Writable working data lives on the /data volume, never inside the image.
    OUTLOOK_DOWNLOAD_DIR=/data/outlook_downloads \
    MANUAL_UPLOAD_DIR=/data/manual_uploads \
    SHAREPOINT_INVOICE_CACHE_DIR=/data/sharepoint_invoice_cache

WORKDIR /app

# Dependencies first so code changes do not reinstall them. The editable install
# keeps the code in /app, which is where the app expects its project root.
COPY pyproject.toml ./
COPY app ./app
RUN pip install -e '.[azure]'

RUN useradd --system --uid 10001 --home-dir /app invoice \
    && mkdir -p /data \
    && chown -R invoice:invoice /data
USER invoice
VOLUME ["/data"]

EXPOSE 8000

# /api/health always answers 200, so check the status field it reports.
# "degraded" only means the worker heartbeat is missing or old; the worker
# waits for web to be healthy before it starts, so treating that as unhealthy
# would stop the worker ever starting on a fresh database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4); sys.exit(0 if json.load(r).get('status') in ('ok', 'degraded') else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
