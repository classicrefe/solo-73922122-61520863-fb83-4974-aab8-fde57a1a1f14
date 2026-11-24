FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/inspection.sqlite3

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# SQLite database lives on a named volume so data survives re-creation.
RUN mkdir -p /data && chown -R 10001:10001 /data /srv
VOLUME ["/data"]

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

# SQLite serialises writers itself; several workers let reads proceed in
# parallel while submissions/corrections queue on the single write lock.
# The control socket is only useful locally and lands under $HOME.
CMD ["gunicorn", "--workers", "4", "--threads", "2", "--no-control-socket", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "app:create_app()"]
