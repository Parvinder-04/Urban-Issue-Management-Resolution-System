"""Application configuration with a canonical, working-directory-independent DB path.

The operational database is selected by ``DATABASE_URL``. When that variable is
not set, local development uses the separate India working copy
``uimrs_india_dev.db`` in the project root. The legacy ``uimrs.db`` and the
protected ``uimrs_india_candidate.db`` are never chosen implicitly.

Frontend selection is a separate concern, controlled only by
``UIMRS_FRONTEND_MODE`` (``india`` or ``legacy``). It never affects API
authorization: every write is still governed by the route-level JWT/RBAC rules.
"""
import os
from pathlib import Path
from functools import lru_cache

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEV_DB_NAME = "uimrs_india_dev.db"
DEV_DB_PATH = PROJECT_ROOT / DEV_DB_NAME
LEGACY_DB_PATH = PROJECT_ROOT / "uimrs.db"
CANDIDATE_DB_PATH = PROJECT_ROOT / "uimrs_india_candidate.db"

# ---------- Frontend selection (independent of the database) ----------
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FRONTEND_PAGES = {
    "india": "uimrs.html",
    "legacy": "index.html",
}
DEFAULT_FRONTEND_MODE = "india"

# Legacy ``UIMRS_CANDIDATE_MODE`` values kept as a frontend-selection alias.
# The alias only chooses a page; it no longer disables any API write operation.
_CANDIDATE_MODE_ALIASES = {
    "1": "india",
    "true": "india",
    "yes": "india",
    "on": "india",
    "0": "legacy",
    "false": "legacy",
    "no": "legacy",
    "off": "legacy",
}


class FrontendModeError(ValueError):
    """Raised when the configured frontend mode is not a known value."""


def resolve_frontend_mode(environ=None) -> str:
    """Resolve the frontend mode without touching database configuration.

    Precedence:
      1. ``UIMRS_FRONTEND_MODE`` (explicit, validated)
      2. ``UIMRS_CANDIDATE_MODE`` (backward-compatible alias)
      3. ``DEFAULT_FRONTEND_MODE`` ("india")

    An unknown value raises :class:`FrontendModeError` instead of silently
    falling back to a different page.
    """
    env = os.environ if environ is None else environ

    explicit = (env.get("UIMRS_FRONTEND_MODE") or "").strip().lower()
    if explicit:
        if explicit not in FRONTEND_PAGES:
            raise FrontendModeError(
                f"Invalid UIMRS_FRONTEND_MODE value '{explicit}'. "
                f"Valid values: {', '.join(sorted(FRONTEND_PAGES))}."
            )
        return explicit

    legacy = (env.get("UIMRS_CANDIDATE_MODE") or "").strip().lower()
    if not legacy:
        return DEFAULT_FRONTEND_MODE

    aliased = _CANDIDATE_MODE_ALIASES.get(legacy)
    if aliased is None:
        raise FrontendModeError(
            f"Invalid UIMRS_CANDIDATE_MODE value '{legacy}'. "
            "Use '1' for the India frontend or '0' for the legacy frontend, "
            "or set UIMRS_FRONTEND_MODE explicitly. "
            f"Valid frontend modes: {', '.join(sorted(FRONTEND_PAGES))}."
        )
    return aliased


def frontend_dashboard_path(mode: str | None = None, environ=None) -> Path:
    """Return the dashboard file for the selected frontend mode."""
    resolved = resolve_frontend_mode(environ) if mode is None else str(mode).strip().lower()
    if resolved not in FRONTEND_PAGES:
        raise FrontendModeError(
            f"Invalid frontend mode '{resolved}'. "
            f"Valid values: {', '.join(sorted(FRONTEND_PAGES))}."
        )
    return FRONTEND_DIR / FRONTEND_PAGES[resolved]


def default_database_url() -> str:
    """Local-development default: the separate India development working copy."""
    return f"sqlite:///{DEV_DB_PATH.as_posix()}"


def resolve_database_url(environ=None) -> str:
    """Return an explicit DATABASE_URL when configured, else the dev default.

    No deployment-specific host or path is hard-coded here: deployments must
    inject DATABASE_URL through their own environment.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("DATABASE_URL") or "").strip()
    return explicit or default_database_url()


# ---------- Secret key safety ----------
# A clearly documented development-only value. It is never accepted in a
# production environment and must never be used for a deployed system.
DEFAULT_DEV_SECRET_KEY = "uimrs-local-development-secret-only-not-for-production"
INSECURE_SECRET_KEYS = {
    "",
    "CHANGE_ME_IN_PRODUCTION",
    "secret",
    "password",
    "changeme",
    DEFAULT_DEV_SECRET_KEY,
}
MIN_SECRET_KEY_LENGTH = 32


class InsecureSecretKeyError(RuntimeError):
    """Raised when a production deployment has no usable SECRET_KEY."""


class CorsConfigurationError(ValueError):
    """Raised when CORS is configured with an unsafe wildcard origin."""


def resolve_environment(environ=None) -> str:
    """Resolve the deployment environment name."""
    env = os.environ if environ is None else environ
    for key in ("UIMRS_ENV", "APP_ENV", "ENVIRONMENT"):
        value = (env.get(key) or "").strip().lower()
        if value:
            return value
    return "development"


def is_production_environment(environment=None, environ=None) -> bool:
    env_name = environment if environment is not None else resolve_environment(environ)
    return env_name in {"production", "prod"}


def is_insecure_secret_key(value) -> bool:
    if not isinstance(value, str):
        return True
    normalized = value.strip()
    return (
        not normalized
        or normalized in INSECURE_SECRET_KEYS
        or len(normalized) < MIN_SECRET_KEY_LENGTH
    )


def resolve_secret_key(environ=None) -> str:
    """Resolve SECRET_KEY, failing clearly in production when it is missing or insecure."""
    env = os.environ if environ is None else environ
    production = is_production_environment(environ=env)
    explicit = (env.get("SECRET_KEY") or "").strip()

    if not explicit:
        if production:
            raise InsecureSecretKeyError(
                "SECRET_KEY is required in production. Set a strong, unique "
                "SECRET_KEY environment variable before starting UIMRS."
            )
        return DEFAULT_DEV_SECRET_KEY

    if is_insecure_secret_key(explicit) and production:
        raise InsecureSecretKeyError(
            "SECRET_KEY is missing, default or too short for production. Set a "
            "strong, unique SECRET_KEY of at least 32 characters."
        )
    return explicit


# ---------- CORS safety ----------
DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://localhost:19006",
]


def resolve_allowed_origins(environ=None) -> list[str]:
    """Resolve the CORS allow-list from the environment.

    Wildcard origins are rejected because credentialed requests cannot be
    safely combined with ``*``. When no explicit list is configured, a hosting
    platform's own public URL (``RENDER_EXTERNAL_URL``) is trusted alongside the
    local development origins, so a deployed instance works without hard-coding
    its hostname in the repository.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("ALLOWED_ORIGINS") or env.get("CORS_ALLOWED_ORIGINS") or "").strip()
    if not raw:
        origins = list(DEFAULT_ALLOWED_ORIGINS)
        hosted_url = (env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        if hosted_url and hosted_url not in origins:
            origins.insert(0, hosted_url)
        return origins

    origins = []
    seen = set()
    for item in raw.split(","):
        origin = item.strip()
        if not origin or origin in seen:
            continue
        if origin == "*":
            raise CorsConfigurationError(
                "Wildcard CORS origins are not allowed with credentialed "
                "requests. Configure explicit origins in ALLOWED_ORIGINS."
            )
        origins.append(origin)
        seen.add(origin)

    if not origins:
        return list(DEFAULT_ALLOWED_ORIGINS)
    return origins


class Settings:
    APP_NAME = "Intelligent Urban Issue Management & Resolution System"
    API_V1_PREFIX = "/api/v1"
    DATABASE_URL = resolve_database_url()
    SECRET_KEY = resolve_secret_key()
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
    DUPLICATE_RADIUS_METERS = 50.0
    HOTSPOT_RADIUS_METERS = 350.0
    HOTSPOT_MIN_ISSUES = 4
    ML_CONFIDENCE_THRESHOLD = 0.40
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", str(PROJECT_ROOT / "uploads"))
    MAX_UPLOAD_SIZE_MB = 10
    ALLOWED_ORIGINS = resolve_allowed_origins()

@lru_cache
def get_settings(): return Settings()
