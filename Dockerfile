# Build the React frontend into static assets.
FROM node:24-alpine AS frontend-build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Serve the assets and proxy API calls to BACKEND_URL at runtime.
FROM node:24-alpine AS frontend
WORKDIR /app
ENV NODE_ENV=production
COPY --from=frontend-build /app/dist ./dist
COPY --from=frontend-build /app/server.mjs ./server.mjs
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8501/healthz >/dev/null || exit 1
CMD ["node", "server.mjs"]

# The backend is the default image target when no --target is supplied.
FROM python:3.12-slim AS backend

# Keep backend logs unbuffered and prevent bytecode-only files in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The backend imports github_infrastructure, api, workers, and workflows from here.
WORKDIR /app

# Copy the backend manifest separately so dependency installation is cacheable.
COPY backend/requirements.txt /app/requirements.txt

# Install API, worker, cloud, GitHub, Slack, and agent dependencies.
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy backend source only; secrets and local environments are excluded by .dockerignore.
COPY backend/ /app/

# Document the FastAPI port used by the API container.
EXPOSE 8000

# Give orchestrators a lightweight liveness probe for the FastAPI health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# Use PORT when supplied by a platform such as Cloud Run, otherwise use local port 8000.
CMD ["sh", "-c", "exec uvicorn github_infrastructure.main:app --host 0.0.0.0 --port=${PORT:-8000}"]
