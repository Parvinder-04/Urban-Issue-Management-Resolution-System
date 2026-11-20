#!/bin/sh
# UIMRS public production entrypoint.
#
# This entrypoint is for the approved FREE deployment architecture only.
# It refuses to start unless a PostgreSQL DATABASE_URL is configured.

set -eu

DATA_DIR="${UIMRS_DATA_DIR:-/var/data}"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL is required and must point to PostgreSQL." >&2
    exit 1
fi

case "$DATABASE_URL" in
    sqlite:*|sqlite3:*)
        echo "ERROR: SQLite is not allowed for production. Configure a PostgreSQL DATABASE_URL." >&2
        exit 1
        ;;
esac

mkdir -p "$DATA_DIR" "$DATA_DIR/uploads"

# Render runs the container as root. Fix ownership, then drop privileges.
if [ "$(id -u)" = "0" ]; then
    chown -R uimrs:uimrs "$DATA_DIR" 2>/dev/null || true
    exec runuser -u uimrs -- sh "$0" "$@"
fi

export PYTHONPATH="${PYTHONPATH:-/app/backend}"
export UPLOAD_DIR="${UPLOAD_DIR:-$DATA_DIR/uploads}"
export EVIDENCE_STORAGE_BACKEND="${EVIDENCE_STORAGE_BACKEND:-database}"

echo "UIMRS: preparing public database at configured PostgreSQL URL"
python /app/scripts/build_public_database.py --database-url "$DATABASE_URL"

echo "UIMRS: starting server on port ${PORT:-8000}"
exec python -m uvicorn app.main:app \
    --app-dir /app/backend \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips '*'
