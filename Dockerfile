FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md /app/
COPY app /app/app
COPY config /app/config
COPY db /app/db
COPY scripts /app/scripts

RUN pip install --no-cache-dir . \
    && chmod +x /app/scripts/start-api.sh \
    && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/v1/health', timeout=4)"

CMD ["./scripts/start-api.sh"]
