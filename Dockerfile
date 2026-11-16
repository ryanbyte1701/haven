# Multi-arch base: builds on Intel/AMD (x86_64) and ARM64 Synology models alike.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HAVEN_CONFIG=/config/config.yaml HAVEN_DATA=/data HOME=/tmp

WORKDIR /srv
COPY requirements.txt .
COPY requirements ./requirements
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Used automatically when /config/config.yaml doesn't exist yet (starts in demo mode)
COPY config/config.example.yaml /srv/config.example.yaml

# Default non-root user. docker-compose.yml overrides this with `user:` so files in
# ./data are owned by your NAS user (Synology: usually 1026:100).
RUN mkdir -p /config /data && chmod 777 /data
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
