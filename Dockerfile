# Image multi-architecture (amd64 / arm64 pour Raspberry Pi 4/5)
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd --create-home --uid 1000 flotte && mkdir -p /data && chown flotte /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Un seul worker : le planificateur intégré (SMS, synchro Getaround, PV) doit tourner une seule fois.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
