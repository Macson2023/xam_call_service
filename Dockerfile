FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_URL=sqlite+aiosqlite:////app/data/calls.db

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

RUN mkdir -p /app/data && chown -R app:app /app
USER app

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081", "--proxy-headers", "--forwarded-allow-ips", "*", "--ws-ping-interval", "20", "--ws-ping-timeout", "20", "--timeout-keep-alive", "30"]
