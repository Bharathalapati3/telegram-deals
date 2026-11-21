FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# build tools are only needed if a wheel is missing for your CPU (e.g. some ARM boards)
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt && pip install cryptg || true

COPY . .

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

VOLUME ["/app/data"]
CMD ["python", "main.py"]
