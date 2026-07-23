FROM python:3.12-slim

LABEL org.opencontainers.image.title="oracle-readiness-mcp" \
      org.opencontainers.image.description="Oracle Cloud Readiness MCP Server — periodic scrape + MCP over HTTP" \
      org.opencontainers.image.version="2.0.0"

WORKDIR /app

# Install Python dependencies (all pure-Python or have pre-built wheels — no gcc needed)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY oracle_scraper.py db.py server.py scheduler.py settings.py auth.py ./
COPY static/ ./static/

# Create non-root user and data directory with correct permissions BEFORE the VOLUME declaration
RUN useradd -m -u 1001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /data /app

ENV READINESS_DATA_DIR=/data \
    READINESS_REFRESH_HOURS=6 \
    READINESS_AUTOSTART_REFRESH=1 \
    READINESS_HTTP_HOST=0.0.0.0 \
    READINESS_HTTP_PORT=8080 \
    PYTHONUNBUFFERED=1

# Declare volume AFTER chown so the mount point inherits correct ownership
VOLUME ["/data"]

EXPOSE 8080

# Health check: poll the /health endpoint
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=8)" || exit 1

USER appuser

CMD ["python", "server.py", "--http"]
