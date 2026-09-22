# Build the complete MangaDock application from the repository root.
FROM python:3.11-slim

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

COPY app.py config.py comic.json /app/
COPY mangadock /app/mangadock
COPY templates /app/templates
COPY static /app/static
# tools/ = 封面超分 worker + 打包的 ONNX 模型（tools/models/）。
# 有 onnxruntime（requirements.txt 已含）就能在容器内 CPU 超分；
# 模型缺失/包缺失时自动降级 Pillow，不会影响启动。
COPY tools /app/tools

RUN mkdir -p /app/comic /app/小说 /app/static/cover /app/instance \
    && test -f /app/comic.json || printf '{}\n' > /app/comic.json

ENV PYTHONPATH=/app
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
# Gunicorn must bind 0.0.0.0 inside the container, or port publishing cannot reach it.
ENV MANGADOCK_BIND=0.0.0.0:5001

EXPOSE 5001

CMD ["python3", "app.py"]
