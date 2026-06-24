FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PODCAST_ASSISTANT_HOST=0.0.0.0 \
    PODCAST_ASSISTANT_ROOT=/app/data \
    PODCAST_DEMO_MODE=1 \
    PODCAST_QUICK_ENGINE=pipeline \
    PODCAST_COMPARISON_ENGINE=deterministic \
    PODCAST_DISABLE_UV=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/网页书架 /app/data/专题项目 /app/jobs

EXPOSE 8765

CMD ["python", "server.py"]
