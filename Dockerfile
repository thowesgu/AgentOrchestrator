FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent_orchestrator/ ./agent_orchestrator/
RUN pip install --no-cache-dir . \
    && useradd --create-home --home-dir /home/app app \
    && chown -R app:app /home/app
USER app
RUN python -m copilot download-runtime --in-process
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
EXPOSE 8000
CMD ["uvicorn", "agent_orchestrator.web:app", "--host", "0.0.0.0", "--port", "8000"]
