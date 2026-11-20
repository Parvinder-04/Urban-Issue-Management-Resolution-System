# Task 15 — Complete Project Audit

**Date:** 2026-11-20
**Scope:** Audit performed before any Task 15 change. No database, credential or
production file was modified during this audit.

---

## 1. Architecture (as found)

| Layer | Implementation |
|---|---|
| Backend | FastAPI 0.141 on Python 3.13, single ASGI app (`backend/app/main.py`) |
| Data access | SQLAlchemy 2.0 ORM, Alembic revisions `0001`–`0004` |
| Database | SQLite file selected by `DATABASE_URL`; SQLite-specific startup integrity guard and backup API |
| Auth | JWT (HS256, 24 h) in the `Authorization: Bearer` header; bcrypt password hashes |
| RBAC | Database roles `CITIZEN`, `OFFICER`, `ADMIN`; server-enforced on every route |
| Frontend | Five server-served HTML pages with inline CSS/JS; no build step |
| Maps | Leaflet 1.9.4 loaded from unpkg CDN on the historical dashboard |
| Images | UPLOAD_DIR filesystem storage with MIME/size checks and authenticated content endpoint |
| Tests | 21 `unittest`/pytest modules, 462 passing tests, isolated in-memory or temp DBs |
| Deployment | Windows launchers (`run_windows.ps1`, `run_production.ps1`, `run_test_environment.ps1`), no container/cloud config |

### Request/response flow

```
Browser page (HTML + inline JS)
   → /api/v1/* (FastAPI routers)
      → JWT dependency (get_current_user / require_role)
      → SQLAlchemy session
         → SQLite database
```

The frontend is same-origin: pages are served by FastAPI, and the JS calls
`/api/v1/*` on the same origin. There is no separate frontend build or dev
server, which makes a single-origin cloud deployment straightforward.

---

## 2. Frontend inventory

| File | Size | Purpose | Key facts |
|---|---|---|---|
| `frontend/index.html` | 16 KB | Legacy authority dashboard | Calls live `/api/v1/analytics/dashboard` and `/issues/dashboard` |
| `frontend/index_india_staging.html` | 23 KB | India historical dashboard | Historical-Bengaluru-only labels, Leaflet maps, source-agency table, chatbot |
| `frontend/citizen_portal.html` | 97 KB | Citizen portal | Register/login, submit, map picker, image upload, My Complaints, history, profile |
| `frontend/officer_portal.html` | 65 KB | Officer portal | Login, department queue scopes, filters, claim, status update, history |
| `frontend/admin_portal.html` | 112 KB | Admin portal | Overview, live/historical management, review queue, routing, officer management, directory, audit log |

All pages use a similar navy/teal visual language already, but each defines its
own CSS variables and sidebar. There is no shared stylesheet, no unified
navigation, no landing page, no analytics page and no 3D gallery.

### Served routes today

| Route | Page |
|---|---|
| `/` | `index_india_staging.html` (historical dashboard) |
| `/citizen` | `citizen_portal.html` |
| `/officer` | `officer_portal.html` |
| `/admin` | `admin_portal.html` |
| `/health` | JSON liveness |
| `/ready` | JSON readiness (DB state + Alembic revision) |

`UIMRS_FRONTEND_MODE=india|legacy` selects the root page. Several tests assert
that `/` returns the historical dashboard byte-for-byte.

---

## 3. Backend/API inventory

All endpoints are prefixed `/api/v1`.

### Authentication — `routers/auth.py`
- `POST /auth/register` — citizens only; role forced server-side.
- `POST /auth/login` — JWT access token.
- `GET /auth/me` — current user.

### Complaints — `routers/issues.py`
- `POST /issues` — create complaint; classification, priority, routing.
- `GET /issues` — role-scoped list with status/category/priority/city filters.
- `GET /issues/dashboard` — public historical-only feed.
- `GET /issues/{id}` — detail with read authorization.
- `GET /issues/{id}/history` — status history.
- `PATCH /issues/{id}/status` — officer/admin only, claim required, transition validated.
- `POST /issues/{id}/images` — evidence upload (size/MIME validated).
- `GET /issues/{id}/images` and `.../content` — authorised evidence access.

### Analytics — `routers/analytics.py`
- `GET /analytics/dashboard` — public **historical-only** aggregate: totals,
  category breakdown, hotspots.
- `GET /analytics/departments` — public **historical-only** source-agency table.
- `GET /analytics/hotspots` — public historical clusters.

### Officer — `routers/officer.py`
- `GET /officer/overview` — department-scoped counts.
- `GET /officer/issues` — scopes `queue|unclaimed|mine|all`, filters.
- `GET /officer/issues/{id}` — department-scoped detail + permissions.
- `POST /officer/issues/{id}/claim` — atomic claim.

### Administrator — `routers/admin.py`
- `GET /admin/overview` — live + historical counts, city/department filters.
- `GET /admin/issues` — live/historical/all, filters, search, pagination.
- `GET /admin/issues/{id}` — detail with `can_route` decision flag.
- `GET /admin/review-queue` — unassigned review work.
- `POST /admin/issues/{id}/route` — route to verified department or leave unassigned; reason required; audit written.
- `GET /admin/officers`, `POST /admin/officers`, `PATCH /admin/officers/{id}`.
- `GET /admin/departments` — read-only verified directory.
- `GET /admin/audit-logs` — paginated, filtered, append-only.

### Chatbot — `routers/chatbot.py`
- `POST /chatbot/message` — pre-approved English/Hindi civic intents.

### System
- `GET /health`, `GET /ready`.

---

## 4. Data model

Core tables: `users`, `departments`, `issues`, `issue_images`,
`issue_status_updates`, `admin_audit_logs`, `alembic_version`.

Critical separation:
- `issues.is_historical = 1` → imported Bengaluru I Change My City/Janaagraha
  records. Public analytics, the public feed and the historical dashboard are
  restricted to these rows. They are immutable through live workflows.
- `issues.is_historical = 0` → live UIMRS complaints. Visible only to their
  reporter, their department or an administrator.
- Synthetic evaluation rows are live rows marked `[EVALUATION TEST]` with
  `EVAL-14A-*` identifiers.

Routing fields: `department_id`, `assigned_officer_id`, `routing_status`
(`assigned|unassigned_review|unassigned_no_mapping`), `routing_reason`.

Classification fields: `category`, `category_confidence`, `category_source`,
`category_model_version`, `category_needs_review`, `category_review_reason`,
`category_alternatives`.

Priority fields: `priority_score`, `priority_level`, `priority_reasons`.

---

## 5. Verified project facts preserved by Task 15

1. Gurugram is the primary operational city.
2. Gurugram complaints are submitted directly through UIMRS; there is no live
   MCG/GMDA API integration.
3. Bengaluru historical records are separate, read-only imported data.
4. Bengaluru historical ML results are not Gurugram model performance.
5. Gurugram classification is deterministic Indian keyword rules
   (`india_keyword_rule`); Bengaluru live classification uses the V2.2 model.
6. Priority is an explainable heuristic, not a calibrated probability.
7. Routing uses the verified department directory with manual review where
   jurisdiction is unverified.
8. Historical records are immutable in live workflows.
9. Coverage is Gurugram + a live Bengaluru model path, not pan-India.
10. Image processing performs quality checks, not AI image recognition.

---

## 6. Current limitations relevant to Task 15

| Limitation | Impact |
|---|---|
| No landing page | Root URL opens a dense historical dashboard, not an evaluator-friendly entry point |
| No unified navigation | Users must know `/citizen`, `/officer`, `/admin` URLs |
| No shared design system | Four separate palettes and component styles |
| No analytics page | Historical analytics are embedded in the dashboard; live analytics only in the admin portal |
| No 3D gallery | Not present |
| CDN dependency | Leaflet loaded from unpkg; a blocked CDN degrades maps |
| SQLite-only guard | Startup integrity guard and backup tooling assume a SQLite file |
| No cloud deployment config | No Dockerfile, `render.yaml`, production start command or seed workflow |
| Historical data redistribution | Prior release decision excludes Bengaluru historical data from public distribution for licensing reasons |

---

## 7. Deployment risk assessment

### Option A — Render Web Service + persistent disk (recommended)
- Preserves SQLite and every existing SQLite-specific guard.
- One origin serves pages and APIs; no CORS or cross-origin auth issues.
- Requires a paid instance and a small persistent disk (approval required).
- Persistent disk holds both the database file and uploaded evidence.

### Option B — Render + managed PostgreSQL
- Needs a reviewed migration of all four Alembic revisions and the SQLite
  integrity guard.
- Requires external object storage for images.
- Higher risk of subtle behaviour changes; not justified for the evaluation
  timeline.

### Option C — free serverless host
- Ephemeral filesystem loses SQLite data and uploads on restart.
- Does not satisfy the required database/image persistence checks.

**Decision:** prepare Option A configuration and instructions. Do **not** create
paid resources or publish anything until the owner approves. The deployment
guide will document Option B as a future path.

### Secrets and data handling
- `SECRET_KEY`, seed passwords and `DATABASE_URL` come from environment
  variables, never source code.
- Public deployment database must not include the historical Bengaluru dataset
  or private complaint records.
- Synthetic evaluation rows must retain `[EVALUATION TEST]`.
- The local test DB, credentials file, backups and model binaries must not be
  published.

---

## 8. Implementation sequence

1. **Shared design system** — `frontend/assets/uimrs.css`; reusable nav, hero,
   card, button, table, footer and responsive rules.
2. **Unified landing page** — `frontend/uimrs.html`; hero, how it works,
   features, supported cities/scope, portals, 3D preview, Future Development.
3. **Server routes** — `/` → landing, `/dashboard` → historical dashboard,
   `/analytics`, `/gallery`, static asset mount.
4. **Analytics page** — historical and live sections, clearly separated,
   role-gated live data, SVG charts and city/category filters.
5. **3D gallery** — vendored Three.js, nine procedural category models, orbit
   controls, reset, lazy model construction, WebGL fallback.
6. **Consistent navigation** — add a compact "UIMRS Home" link to each portal
   sidebar without changing portal behaviour.
7. **Deployment preparation** — Dockerfile, `render.yaml`, start/seed scripts,
   production DB builder, guide. No resource creation.
8. **Testing** — update route expectations, add landing/analytics/gallery tests,
   run the full suite, capture screenshots.
9. **Approval gate** — present the prepared deployment and request explicit
   approval before creating any paid resource.

---

## 9. Files expected to change

New:
- `frontend/uimrs.html`
- `frontend/analytics.html`
- `frontend/gallery.html`
- `frontend/assets/uimrs.css`
- `frontend/assets/uimrs-nav.js`
- `frontend/vendor/three/*`
- `deploy/*` (Dockerfile, render.yaml, start script, seed data)
- `scripts/build_public_database.py`
- `docs/TASK15_*.md`
- Task 15 tests

Modified:
- `backend/app/main.py` (new routes + static mount)
- `backend/app/core/config.py` (`india` root page → landing)
- `backend/requirements.txt` (production host only if needed; no new app deps)
- Portal HTML files (home link only)
- Existing route tests that assert `/` is the historical dashboard

Not modified:
- Database schema or Alembic revisions
- Classification, priority, routing, auth or RBAC logic
- Any protected database, backup, forensic record or credential
