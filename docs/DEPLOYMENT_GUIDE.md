# UIMRS Deployment Guide

## Supported Windows setup

- Windows 10/11
- Python 3.11 or newer
- A virtual environment created in `backend/venv`
- SQLite for local development

## Dependency installation

```powershell
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Environment configuration

Copy `.env.example` to `.env` and set at least:

- `SECRET_KEY` (required in production)
- `UIMRS_ENV=production` for production-style startup
- `UIMRS_FRONTEND_MODE=india`
- `DATABASE_URL` if not using the default development database
- `ALLOWED_ORIGINS` for non-same-origin frontends
- `UPLOAD_DIR` if uploads must be stored outside the project folder

Development may use the documented development-only fallback secret.

## Development startup

```powershell
.\run_windows.ps1
```

This validates configuration and database readiness, then starts Uvicorn with
reload enabled on `127.0.0.1:8000` by default.

## Production-style local startup

```powershell
$env:SECRET_KEY = "replace-with-a-strong-unique-secret"
$env:UIMRS_ENV = "production"
.\run_production.ps1
```

This starts Uvicorn without `--reload`. Configure `UIMRS_HOST` and
`UIMRS_PORT` if needed.

## Database migration

```powershell
backend\venv\Scripts\python.exe scripts/migrate_database.py
```

Do not use `Base.metadata.create_all()` on an existing database.

## Health and readiness checks

- `GET /health` — basic application health.
- `GET /ready` — database connection, schema and Alembic revision check.

Readiness returns 200 when ready and 503 when the database or schema is
unavailable.

## Evidence storage

- Database: SQLite file (default `uimrs_india_dev.db`).
- Uploads: `UPLOAD_DIR`, with evidence under `uploads/evidence`.
- Evidence files are not publicly mounted; downloads require authentication.
- Both the database and upload directory must persist and be backed up together.

## Backup procedure

```powershell
backend\venv\Scripts\python.exe scripts/backup_uimrs.py
```

The backup is timestamped, never overwrites an existing backup, includes a
manifest with SHA-256 hashes and verifies integrity.

## Restore procedure

Validate a backup first:

```powershell
backend\venv\Scripts\python.exe scripts/restore_uimrs.py --backup-dir backups\uimrs_backup_YYYYMMDD_HHMMSS
```

Restore into a separate target directory only after explicit confirmation:

```powershell
backend\venv\Scripts\python.exe scripts/restore_uimrs.py --backup-dir backups\uimrs_backup_YYYYMMDD_HHMMSS --confirm RESTORE
```

The restore utility never overwrites the live database or upload directory
automatically.

## Troubleshooting

- Production startup fails if `SECRET_KEY` is missing or insecure.
- Readiness returns 503 if the database is missing, corrupt or has the wrong
  schema/revision.
- If CORS requests fail, set `ALLOWED_ORIGINS` explicitly.
- If evidence uploads fail, check `UPLOAD_DIR` permissions and available space.

## Security precautions

- Never commit `.env`.
- Use a strong, unique `SECRET_KEY` in production.
- Do not expose database files or upload directories publicly.
- Preserve RBAC and image validation.
- Back up the database and evidence directory together.

## Future cloud-hosting requirements

- Persistent storage for the SQLite database or migration to a server database.
- Persistent storage for uploads/evidence.
- TLS termination and reverse proxy.
- Process manager instead of development reload.
- Log monitoring and backup automation.

Multiple application instances require a different database and storage
architecture. A temporary or ephemeral filesystem is unsuitable without
persistent storage.
