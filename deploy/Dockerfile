# UIMRS public deployment image (Task 15).
#
# Build from the repository root so the backend, frontend, Alembic revisions and
# deployment seed are all available:
#
#   docker build -f deploy/Dockerfile -t uimrs .
#
# The image never contains a database, an upload, a .env file or a virtual
# environment: all state lives on the mounted persistent disk.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend \
    UIMRS_ENV=production \
    UIMRS_FRONTEND_MODE=india \
    PORT=8000

WORKDIR /app

# OpenCV (used by evidence-image quality checks) and scikit-learn need these
# runtime libraries. No compiler toolchain is required because every Python
# dependency is installed from a binary wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
COPY backend/requirements_free.txt /app/backend/requirements_free.txt
RUN python -m pip install --no-cache-dir \
        -r /app/backend/requirements.txt \
        -r /app/backend/requirements_free.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY scripts/build_public_database.py /app/scripts/build_public_database.py
COPY deploy /app/deploy
COPY alembic.ini /app/alembic.ini

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin uimrs \
    && mkdir -p /var/data/uploads \
    && chown -R uimrs:uimrs /var/data \
    && chmod +x /app/deploy/start.sh

EXPOSE 8000

# The entrypoint creates the public database on the persistent disk if it does
# not exist yet, seeds the verified directory, then starts uvicorn.
CMD ["sh", "/app/deploy/start.sh"]
