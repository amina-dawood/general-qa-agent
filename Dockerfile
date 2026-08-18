# syntax=docker/dockerfile:1.7

FROM node:24-alpine AS frontend
WORKDIR /ui
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEEPEVAL_TELEMETRY_OPT_OUT=1 \
    PYTHONPATH=/app/backend
WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY --from=frontend /ui/dist /app/dashboard/dist
COPY .env.example /app/.env.example
RUN mkdir -p /app/data /app/data/uploads /app/data/reports

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python", "/app/backend/run_server.py"]
