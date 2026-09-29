FROM python:3.12-slim-bookworm

# Runtime libraries only: WeasyPrint (PDF reports) needs Pango/HarfBuzz and
# fonts. psycopg2-binary and python-snap7 ship their own native libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libharfbuzz-subset0 \
        fontconfig \
        fonts-dejavu-core \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# XDG_CACHE_HOME: fontconfig needs a writable cache dir for the non-root user
ENV TZ=Asia/Kolkata \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    LOG_DIR=/app/logs \
    BACKUP_DIR=/app/Backups \
    BACKUP_LOG_PATH=/app/Backups/backup_log.json

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Non-root user; it owns only the folders the app writes to.
RUN useradd --system --uid 1000 --home-dir /app appuser \
    && mkdir -p /app/logs /app/Backups /app/data_files \
    && chown -R appuser:appuser /app/logs /app/Backups /app/data_files
USER appuser

EXPOSE 5000 8050

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/plc_status', timeout=4)" || exit 1

# ONE worker only (see wsgi.py) - the PLC monitor thread must exist once.
# gthread keeps the worker heartbeat alive during long PDF/Excel requests.
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "8", \
     "--timeout", "300", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
