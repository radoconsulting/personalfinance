# centen — Belgian personal finance for Claude, as a small server (NAS, home server, Raspberry Pi).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CENTEN_HOME=/data TZ=Europe/Brussels

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE constraints.txt ./
COPY centen ./centen
RUN pip install -c constraints.txt .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8770
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/healthz', timeout=4)"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["centen", "web", "--host", "0.0.0.0", "--port", "8770"]
