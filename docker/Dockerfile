FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LM_BRIDGE_DATA_DIR=/data \
    LM_BRIDGE_PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-noto-cjk \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-glib-1-2 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lmarenabridge

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && camoufox fetch

COPY src ./src
COPY docker-entrypoint.py ./

RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
  CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=8)); raise SystemExit(0 if r.get('status') in ('healthy','degraded') else 1)"

ENTRYPOINT ["python", "/opt/lmarenabridge/docker-entrypoint.py"]
CMD ["python", "-m", "src.main"]
