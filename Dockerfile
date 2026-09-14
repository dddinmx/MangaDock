FROM python:3.9-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libjpeg62-turbo \
        zlib1g \
        libpng16-16 \
        libfreetype6 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Runtime content is mounted by docker-compose and is excluded by .dockerignore.
COPY app.py config.py comic.json /app/
COPY mangadock /app/mangadock
COPY templates /app/templates
COPY static /app/static

RUN mkdir -p /app/comic /app/小说 /app/static/cover /app/instance \
    && find /app -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find /app -type f -name '._*' -delete

ENV PYTHONPATH=/app \
    FLASK_APP=app.py \
    FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    MANGADOCK_BIND=0.0.0.0:5001

EXPOSE 5001

CMD ["python3", "app.py"]
