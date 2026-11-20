# UIMRS — Intelligent Urban Issue Management & Resolution System

AI-assisted civic issue reporting, prioritization, duplicate clustering and hotspot analytics for Indian cities (Gurugram and Bengaluru).

## Current build

This repository contains:

- FastAPI backend with JWT authentication and role-based access control
- Citizen, officer and administrator portals
- AI-assisted complaint classification and explainable priority scoring
- City-aware department routing and verified department directory
- Authenticated image upload and verification
- Duplicate clustering and hotspot analytics
- Historical Bengaluru dashboard data (read-only, never mixed with live data)
- Alembic-managed SQLite development database

The application is real-data only. It does not seed test complaints, citizens or officers.

## Database

Local development uses a separate SQLite working copy:

`uimrs_india_dev.db` in the project root.

Protected databases that must never be modified:

- `uimrs.db` — legacy database
- `uimrs_india_candidate.db` — historical Bengaluru candidate database

The database URL can be overridden with `DATABASE_URL`. When it is not set, the
application uses `uimrs_india_dev.db`.

## Environment configuration

Set these variables before starting the application:

- `DATABASE_URL` — optional; defaults to `sqlite:///.../uimrs_india_dev.db`
- `UIMRS_FRONTEND_MODE` — `india` (default) or `legacy`
- `SECRET_KEY` — required in production; use a strong, unique value of at least 32 characters
- `ALLOWED_ORIGINS` — optional comma-separated CORS origins
- `UPLOAD_DIR` — optional upload directory
- `UIMRS_ENV` — set to `production` or `prod` to enforce production secret-key rules

Local development may use the documented development secret. Production startup
fails if `SECRET_KEY` is missing, default or too short.

## Migrations

Schema changes are managed with Alembic. Apply the reviewed migration with:

```bash
backend/venv/Scripts/python.exe scripts/migrate_database.py
```

Do not use `Base.metadata.create_all()` on an existing database.

## Task 15 unified website

The unified public site is served from `/` (`frontend/uimrs.html`). The
historical Bengaluru dashboard moved to `/dashboard`. Additional routes:

- `/analytics` — public historical analytics plus role-gated live analytics.
- `/gallery` — interactive Three.js 3D issue gallery (nine categories).
- `/citizen`, `/officer`, `/admin` — portals (unchanged).
- `/health`, `/ready` — service health and database readiness.

Shared design system: `frontend/assets/uimrs.css` and
`frontend/assets/uimrs-nav.js`. Three.js r160 is vendored locally at
`frontend/vendor/three/` (MIT).

Deployment preparation lives in `deploy/` and `scripts/build_public_database.py`.
See `docs/TASK15_DEPLOYMENT_GUIDE.md`. No cloud resource has been created;
explicit owner approval is required before provisioning the paid Render service.

Task 15 documentation: `docs/TASK15_AUDIT.md`,
`docs/TASK15_IMPLEMENTATION_REPORT.md`, `docs/TASK15_TEST_REPORT.md`,
`docs/TASK15_DEPLOYMENT_GUIDE.md`, `docs/TASK15_3D_ATTRIBUTION.md`.


```powershell
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
cd ..
$env:SECRET_KEY = "replace-with-a-strong-development-secret"
$env:UIMRS_FRONTEND_MODE = "india"
backend\venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open:

- `http://127.0.0.1:8000/` — historical Bengaluru dashboard
- `http://127.0.0.1:8000/citizen` — citizen portal
- `http://127.0.0.1:8000/officer` — officer portal
- `http://127.0.0.1:8000/admin` — administrator portal
- `http://127.0.0.1:8000/docs` — API documentation
- `http://127.0.0.1:8000/health` — health check

## Running tests

From the project root:

```powershell
backend\venv\Scripts\python.exe -B -m unittest discover -s tests
```

Tests use isolated temporary databases and never modify the operational
development database.

## Deployment

See `docs/DEPLOYMENT_GUIDE.md` for supported Windows setup, environment
configuration, startup, migrations, health/readiness checks, evidence storage,
backup, restore and troubleshooting.

Portal and service URLs:

- `/` — historical Bengaluru dashboard
- `/citizen` — citizen portal
- `/officer` — officer portal
- `/admin` — administrator portal
- `/docs` — API documentation
- `/health` — basic health check
- `/ready` — database readiness check

## Officer provisioning

```powershell
backend\venv\Scripts\python.exe scripts/provision_officer.py
```

Officers are provisioned only into existing, active departments in supported
cities. Public officer registration is not available.

## Test environment

For the final evaluation, use the isolated test copy described in
`docs/UIMRS_TEST_RUNBOOK.md`. It is created with:

```powershell
backend\venv\Scripts\python.exe scripts\test_environment.py create
```

Once it exists, start it with one command:

```powershell
.\start_test_environment.cmd
```

The launcher selects `test_environment/uimrs_test.db` and
`test_environment/uploads`, reuses a persistent local `SECRET_KEY`, and refuses
to fall back to the development database. The test database and evidence live in
`test_environment/`, which is excluded from Git. The operational databases are
never modified.

## Project flow

Citizen report -> text/image/location -> AI classification -> priority score ->
duplicate clustering -> department routing -> officer claim and status updates ->
administrator review and audit -> hotspot analytics.
