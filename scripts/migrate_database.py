"""Apply controlled Alembic migrations to the configured UIMRS database.

Usage (from the project root):

    backend/venv/Scripts/python.exe scripts/migrate_database.py
    backend/venv/Scripts/python.exe scripts/migrate_database.py --status
    backend/venv/Scripts/python.exe scripts/migrate_database.py --database-url sqlite:///path/to/db

The URL defaults to the application ``DATABASE_URL`` (or the documented local
development database when that variable is unset). No database path is baked
into the migration scripts themselves.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db_migrate import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
