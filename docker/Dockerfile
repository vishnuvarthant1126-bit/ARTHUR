# ARTHUR in a container.  NOT BUILT YET - Docker Desktop isn't installed on the development
# machine. Everything that can be checked without Docker is checked by
# tests/unit/test_docker_files.py; the first real build is described in docs/DOCKER.md.
#
#   docker compose build            (from the project folder)
#
# What is inside: Python 3.12, ARTHUR's code and its web page. What is NOT inside: your
# .env, your data, the language models (Ollama stays on Windows and keeps the GPU).
FROM python:3.12-slim-bookworm

# No .pyc files in the image, logs appear immediately, pip stays quiet and leaves no cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# tzdata: so TZ=... gives reminders the right local time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Requirements first: this slow layer is reused until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Optional: ARTHUR's own Chromium for the browser tools (+ ~500 MB).
#   docker compose build --build-arg INSTALL_BROWSER=true
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        python -m playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Only the code and the web page - never the whole folder (see .dockerignore as well).
COPY app/ app/
COPY frontend/ frontend/

# An ordinary user without admin rights; it owns only the folders ARTHUR writes to.
RUN useradd --create-home --uid 1000 arthur \
    && mkdir -p /app/data /files /models /voices \
    && chown -R arthur:arthur /app/data /files /models
USER arthur

EXPOSE 8000

# 127.0.0.1 here is the container itself; the answer says whether the server is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# 0.0.0.0 = listen on the container's own network card. That is NOT your Wi-Fi: Compose
# publishes the port on the PC's 127.0.0.1 only (docker-compose.yml).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
