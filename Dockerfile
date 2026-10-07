FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# System deps (tzdata for correct timestamps in reports/notifications).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (better layer caching).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application code.
COPY . .

# Directories used at runtime.
RUN mkdir -p reports data credentials

# Default entry point: run everything once (override in docker-compose).
ENTRYPOINT ["python", "-m", "src.pipeline"]
CMD ["--help"]
