# UIMRS Deployment Guide

This guide covers local and small-scale deployment of the public release. For first-time
setup, read [`UIMRS_PUBLIC_SETUP_GUIDE.md`](UIMRS_PUBLIC_SETUP_GUIDE.md) first.

---

## 1. Supported environment

- Windows 10/11 for the supplied scripts;
- Python 3.11–3.13 (3.13 verified);
- SQLite for local/academic use;
- a virtual environment in `backend/venv`.

The application also runs on Linux/macOS with equivalent commands, but the supplied
launchers are PowerShell scripts.

---

## 2. Dependency installation

```powershell
py -3.13 -m venv backend\venv
backend\venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

---

## 3. Environment configuration

Copy `.env.example` to `.env` only if your process manager reads it. The supplied
launchers set the values for the current process.

Required or recommended variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | JWT signing key; required in production and at least 32 characters |
| `UIMRS_ENV` | `development` or `production` |
| `UIMRS_FRONTEND_MODE` | `india` (default) or `legacy` |
| `DATABASE_URL` | SQLite URL, default `sqlite:///.../local_data/uimrs_public.db` |
| `UPLOAD_DIR` | Evidence directory, default `.../local_data/uploads` |
| `ALLOWED_ORIGINS` | Explicit CORS origins for a separately hosted frontend |
| `MAX_UPLOAD_SIZE_MB` | Upload size limit |

Production startup fails if `SECRET_KEY` is missing, the documented development value, or
shorter than 32 characters.

---

## 4. Database initialization

Create the empty database with Alembic:

```powershell
backend\venv\Scripts\python.exe scripts\migrate_database.py `
  --database-url "sqlite:///C:/path/to/UIMRS/local_data/uimrs_public.db"
```

Check the revision:

```powershell
backend\venv\Scripts\python.exe scripts\migrate_database.py --status
```

Seed the public starter directory:

```powershell
backend\venv\Scripts\python.exe scripts\seed_public_departments.py
```

The seed script is separate from application bootstrap. Bootstrap never creates
departments or complaints.

---

## 5. Starting the application

### Development-style

```powershell
.\start_public_environment.cmd
```

This validates the database and starts Uvicorn on `127.0.0.1:8000`.

### Production-style local start

```powershell
$env:UIMRS_ENV = "production"
.\run_production.ps1
```

`run_production.ps1` requires a secret key and does not use the development fallback.

For a real deployment, run Uvicorn behind a reverse proxy and a process supervisor, use
a strong secret from a secret manager, and replace SQLite with a supported server
database. Those steps are outside the scope of this academic release.

---

## 6. Portal and service URLs

| URL | Purpose |
|---|---|
| `/` | Historical dashboard |
| `/citizen` | Citizen portal |
| `/officer` | Officer portal |
| `/admin` | Administrator portal |
| `/docs` | Interactive API documentation |
| `/redoc` | ReDoc API documentation |
| `/health` | Basic liveness |
| `/ready` | Database readiness and revision check |

---

## 7. Health and readiness

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/ready` returns HTTP 200 only when:

- the configured database is readable;
- the SQLite integrity check passes;
- every required table and column exists;
- the Alembic revision is `0004_admin_audit_log`;
- a trivial query succeeds.

Otherwise it returns HTTP 503 with a generic message.

---

## 8. Accounts

- Citizens self-register at `/citizen`.
- Administrators are created with `scripts/provision_public_admin.py`.
- Officers are created with `scripts/provision_officer.py` against an existing active
  department.

Passwords are entered through hidden prompts, hashed with bcrypt and never printed.
There is no public officer or administrator registration.

---

## 9. Evidence storage

`UPLOAD_DIR` defaults to `local_data/uploads`. Evidence is stored under `evidence/` and
served only through authenticated endpoints. The public release contains no evidence
files.

Protect the upload directory with filesystem permissions. Do not serve it directly from a
web server.

---

## 10. Backup and restore

Backup:

```powershell
$env:DATABASE_URL = "sqlite:///C:/path/to/UIMRS/local_data/uimrs_public.db"
$env:UPLOAD_DIR = "C:\path\to\UIMRS\local_data\uploads"
backend\venv\Scripts\python.exe scripts\backup_uimrs.py
```

The backup contains a transactionally consistent SQLite copy, the evidence directory and a
manifest with SHA-256 hashes. Existing backup directories are never overwritten.

Restore into a new directory:

```powershell
backend\venv\Scripts\python.exe scripts\restore_uimrs.py `
  --backup-dir <BACKUP_DIR> --target-dir <NEW_DIR>
```

The restore utility validates the manifest, hashes, schema and revision. It does not
overwrite a live database. Review the restored data before promoting it.

---

## 11. Security checklist

- [ ] A unique `SECRET_KEY` of at least 32 characters is set outside the repository.
- [ ] `UIMRS_ENV=production` is set for production-style runs.
- [ ] `ALLOWED_ORIGINS` contains explicit origins only.
- [ ] `local_data/`, `.env`, databases, uploads and backups are Git-ignored.
- [ ] Administrator and officer accounts use strong, unique passwords.
- [ ] The server is bound to a trusted interface or placed behind a reverse proxy.
- [ ] Evidence files are protected by filesystem permissions.
- [ ] The deployment does not include private complaint data without documented
      permission.
- [ ] No model artifact is installed without documented permission.

---

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| `/ready` returns 503 | `DATABASE_URL`, schema, revision and SQLite integrity |
| `SECRET_KEY is required in production` | Set a strong `SECRET_KEY` |
| Port already in use | Stop the owning process or use another port |
| No departments during officer provisioning | Run `scripts/seed_public_departments.py` |
| Bengaluru complaints need review | Model artifact is not included; install an authorised model if permitted |
| Dashboard is empty | Expected with no historical dataset; see the public setup guide |

For first-time setup issues, see the troubleshooting section of
[`UIMRS_PUBLIC_SETUP_GUIDE.md`](UIMRS_PUBLIC_SETUP_GUIDE.md).
