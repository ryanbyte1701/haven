# Multi-arch base: builds on Intel/AMD (x86_64) and ARM64 Synology models alike.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HAVEN_CONFIG=/config/config.yaml HAVEN_DATA=/data HOME=/tmp \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /srv
# iproute2 (`ip`): only used by high availability to add/remove the virtual IP (deploy/synology/docker-compose.ha.yml).
# Its own layer so code/requirement changes don't re-download it.
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY requirements ./requirements
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Defaults used when /config/config.yaml doesn't exist (no demo devices; MQTT, Ecowitt and history on)
COPY config/config.example.yaml /srv/config.example.yaml

# Default non-root user. docker-compose.yml overrides this with `user:` so files in
# ./data are owned by your NAS user (Synology: usually 1026:100).
# /data is a mounted host folder in practice; the in-image one only needs to suit the default user.
RUN mkdir -p /config /data && chown 1000:1000 /data && chmod 700 /data
USER 1000:1000

# 1701 = HTTP (dashboard, Ecowitt, /metrics)   1702 = HTTPS (self-signed unless you mount a cert)
EXPOSE 1701 1702
# Shell form (sh -c), so the check follows HAVEN_PORT; 127.0.0.1 because "localhost" may resolve to ::1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${HAVEN_PORT:-1701}/api/health', timeout=4)"
# Ports, HTTPS and certificates are set with HAVEN_* variables: see app/serve.py and docker-compose.yml.
# X-Forwarded-* headers are trusted only from FORWARDED_ALLOW_IPS (uvicorn reads the variable).
CMD ["python", "-m", "app.serve"]
