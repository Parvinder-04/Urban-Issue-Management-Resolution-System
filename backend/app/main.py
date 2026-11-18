"""UIMRS API entrypoint — real-data-only build.

Frontend page selection (``UIMRS_FRONTEND_MODE``) is independent of API access
control. No global method-based write lock exists: every write is authorized by
the router-level JWT/RBAC checks.
"""
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import (
    FRONTEND_DIR,
    FrontendModeError,
    frontend_dashboard_path,
    get_settings,
)
from app.routers import issues, auth, analytics, chatbot, officer, admin
from app.database import engine
from app.bootstrap import ensure_bootstrap
from app.db_guard import guard_startup, sqlite_path_from_url
from app.db_migrate import HEAD_REVISION, current_revision, prepare_database

settings=get_settings()
app=FastAPI(title=settings.APP_NAME, description="AI-powered civic issue reporting and resolution platform.", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
app.include_router(issues.router, prefix=settings.API_V1_PREFIX)
app.include_router(analytics.router, prefix=settings.API_V1_PREFIX)
app.include_router(chatbot.router, prefix=settings.API_V1_PREFIX)
app.include_router(officer.router, prefix=settings.API_V1_PREFIX)
app.include_router(admin.router, prefix=settings.API_V1_PREFIX)


@app.on_event("startup")
def validate_frontend_configuration():
    # An invalid mode must stop the app rather than silently serving a page
    # nobody asked for. Validated once, before the database guard runs.
    try:
        frontend_dashboard_path()
    except FrontendModeError as exc:
        raise RuntimeError(f"Frontend configuration error: {exc}") from exc


@app.on_event("startup")
def initialize_database():
    # Fail loudly on a corrupt or schema-incompatible database. A brand-new,
    # completely empty database is initialised through Alembic (never through
    # create_all), so it is versioned from the very first run. Existing
    # databases are never altered here: an operator must run the reviewed
    # migration explicitly (scripts/migrate_database.py).
    sqlite_path = sqlite_path_from_url(settings.DATABASE_URL)
    if sqlite_path is not None:
        # SQLite cannot create a missing parent directory. Creating the
        # directory for the configured local database does not create or
        # modify the database itself.
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    state = guard_startup(engine, settings.DATABASE_URL)
    if state == "uninitialized":
        prepare_database(settings.DATABASE_URL)
    ensure_bootstrap()

@app.get("/")
def root():
    try:
        dashboard = frontend_dashboard_path()
    except FrontendModeError as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Frontend configuration error: {exc}"},
        )
    return FileResponse(dashboard) if dashboard.exists() else {"status":"ok","service":settings.APP_NAME}

CITIZEN_PORTAL_PAGE = FRONTEND_DIR / "citizen_portal.html"
OFFICER_PORTAL_PAGE = FRONTEND_DIR / "officer_portal.html"
ADMIN_PORTAL_PAGE = FRONTEND_DIR / "admin_portal.html"


@app.get("/admin")
def admin_portal():
    """Serve the authenticated administrator oversight portal.

    ``/`` remains the historical Bengaluru dashboard, ``/citizen`` remains the
    citizen portal and ``/officer`` remains the officer portal. Every
    ``/api/v1/admin/*`` endpoint independently enforces the ADMIN database role.
    """
    if not ADMIN_PORTAL_PAGE.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": "Administrator portal page not found."},
        )
    return FileResponse(ADMIN_PORTAL_PAGE)


@app.get("/officer")
def officer_portal():
    """Serve the authenticated municipal officer portal.

    ``/`` remains the public historical Bengaluru dashboard and ``/citizen``
    remains the citizen portal. The officer page is a separate interface.
    """
    if not OFFICER_PORTAL_PAGE.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": "Officer portal page not found."},
        )
    return FileResponse(OFFICER_PORTAL_PAGE)


@app.get("/citizen")
def citizen_portal():
    """Serve the authenticated citizen portal.

    This is deliberately separate from ``/``: the historical Bengaluru
    dashboard remains a public, read-only page and never mixes with live
    citizen submissions.
    """
    if not CITIZEN_PORTAL_PAGE.exists():
        return JSONResponse(
            status_code=404,
            content={"detail": "Citizen portal page not found."},
        )
    return FileResponse(CITIZEN_PORTAL_PAGE)


@app.get("/health")
def health(): return {"status":"healthy"}


@app.get("/ready")
def ready():
    """Read-only readiness check for the configured database."""
    try:
        state = guard_startup(engine, settings.DATABASE_URL)
        if state != "validated":
            raise RuntimeError("database is not initialized")
        revision = current_revision(settings.DATABASE_URL)
        if revision != HEAD_REVISION:
            raise RuntimeError("database revision mismatch")
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {
            "status": "ready",
            "database": "ok",
            "revision": revision,
        }
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "detail": "Database readiness check failed."},
        )
