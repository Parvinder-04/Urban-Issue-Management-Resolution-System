"""UIMRS API entrypoint — real-data-only build.

Frontend page selection (``UIMRS_FRONTEND_MODE``) is independent of API access
control. No global method-based write lock exists: every write is authorized by
the router-level JWT/RBAC checks.
"""
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import (
    FRONTEND_DIR,
    FrontendModeError,
    frontend_dashboard_path,
    get_settings,
)
from app.routers import issues, auth, analytics, chatbot, officer, admin
from app.database import SessionLocal, engine
from app.bootstrap import ensure_bootstrap
from app.db_guard import guard_startup
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
HISTORICAL_DASHBOARD_PAGE = FRONTEND_DIR / "index_india_staging.html"
ANALYTICS_PAGE = FRONTEND_DIR / "analytics.html"
GALLERY_PAGE = FRONTEND_DIR / "gallery.html"

ASSETS_DIR = FRONTEND_DIR / "assets"
VENDOR_DIR = FRONTEND_DIR / "vendor"

# Static design-system assets and the locally vendored Three.js build. Both
# directories are served read-only; no uploaded evidence lives here.
if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
if VENDOR_DIR.is_dir():
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")


def _serve_page(path, detail: str):
    """Serve a static HTML page, or a clear JSON 404 when it is missing."""
    if not path.exists():
        return JSONResponse(status_code=404, content={"detail": detail})
    return FileResponse(path)


@app.get("/dashboard")
def historical_dashboard():
    """Public historical Bengaluru dashboard.

    Task 15 moved the unified landing page to ``/``. The historical dashboard
    keeps its read-only data and behaviour and is now served at ``/dashboard``.
    """
    if HISTORICAL_DASHBOARD_PAGE.exists():
        return FileResponse(HISTORICAL_DASHBOARD_PAGE)
    return _serve_page(
        FRONTEND_DIR / "index.html",
        "Historical dashboard page not found.",
    )


@app.get("/analytics")
def analytics_page():
    """Public historical analytics with role-gated live complaint panels."""
    return _serve_page(ANALYTICS_PAGE, "Analytics page not found.")


@app.get("/gallery")
def gallery_page():
    """Interactive Three.js gallery of the nine civic issue categories."""
    return _serve_page(GALLERY_PAGE, "3D gallery page not found.")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # The HTML pages embed an inline SVG favicon; this avoids a noisy 404.
    return Response(status_code=204)


@app.get("/admin")
def admin_portal():
    """Serve the authenticated administrator oversight portal.

    ``/`` is the unified public landing page and ``/dashboard`` serves the
    historical Bengaluru dashboard. ``/citizen`` remains the citizen portal and
    ``/officer`` remains the officer portal. Every ``/api/v1/admin/*`` endpoint
    independently enforces the ADMIN database role.
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

    ``/`` is the unified public landing page, ``/dashboard`` is the public
    historical Bengaluru dashboard and ``/citizen`` remains the citizen portal.
    The officer page is a separate interface.
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

    This is deliberately separate from ``/`` (the public landing page) and
    ``/dashboard`` (the public historical page): live citizen submissions never
    mix with imported historical records.
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
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
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
