# UIMRS Architecture

This document describes the **implemented** public release. It deliberately avoids the
aspirational Flutter/React architecture that appeared in early design notes; the actual
frontend in this release is static HTML/CSS/JavaScript served by FastAPI.

---

## 1. Runtime topology

```
+--------------------------- Browser ---------------------------+
|  /            historical dashboard (public, read-only)         |
|  /citizen     citizen portal (JWT, CITIZEN role)               |
|  /officer     officer portal (JWT, OFFICER role)               |
|  /admin       administrator portal (JWT, ADMIN role)           |
+-------------------------------+-------------------------------+
                                |
                                | HTTPS / JSON
                                v
+--------------------------- FastAPI ----------------------------+
|  app.main                                                      |
|    /health, /ready, portal file responses                      |
|  app.routers                                                   |
|    auth, issues, analytics, chatbot, officer, admin            |
|  app.services                                                  |
|    classification, priority, department routing, geo clustering |
|    evidence storage, image verification, officer workflow,     |
|    admin operations, admin audit                               |
|  app.core.config                                               |
|    database URL, secret key, CORS, frontend mode               |
|  app.db_guard / app.db_migrate                                 |
|    schema validation, SQLite integrity, Alembic runner         |
+-------------------------------+-------------------------------+
                                |
                                v
+--------------------------- SQLite -----------------------------+
|  local_data/uimrs_public.db (default)                          |
|  users, departments, issues, issue_images,                     |
|  issue_status_updates, admin_audit_logs, alembic_version       |
+----------------------------------------------------------------+
```

Evidence files are stored under `UPLOAD_DIR` (default
`local_data/uploads/evidence`) and are served only through authenticated endpoints.

---

## 2. Application startup

`app.main` performs two startup checks:

1. **Frontend configuration** — `UIMRS_FRONTEND_MODE` must be `india` or `legacy`;
   an invalid value stops the app.
2. **Database guard** — `guard_startup` inspects the configured database:
   - no tables → `uninitialized`, then `prepare_database` applies Alembic migrations;
   - valid tables and columns → `validated`;
   - corrupt file → integrity error;
   - missing/extra schema → schema error.

`ensure_bootstrap` then creates only the system service user
(`system@uimrs.local`, non-login, system-managed password marker). It never seeds
departments, complaints, citizens or officers.

`/ready` repeats the guard and revision check read-only and returns HTTP 503 if anything
is wrong.

---

## 3. Data model (summary)

| Table | Purpose |
|---|---|
| `users` | Citizens, officers and administrators; bcrypt password hashes; role and optional department link |
| `departments` | Verified directory rows with city, category, contacts, jurisdiction, routing mode and verification provenance |
| `issues` | Live and historical complaints; category, priority, routing, duplicate, source and explainability fields |
| `issue_images` | Evidence metadata and stored paths |
| `issue_status_updates` | Immutable status history per issue |
| `admin_audit_logs` | Immutable administrative action log |
| `alembic_version` | Applied schema revision |

Historical rows are marked `is_historical = 1`. Public analytics filter on that flag and
never expose live citizen submissions.

---

## 4. Complaint submission flow

1. `POST /api/v1/issues` authenticates the citizen.
2. Input is validated (title, description, city, coordinates, optional evidence).
3. `combine_complaint_text` joins title and description.
4. `canonical_city` normalises Bengaluru/Gurugram aliases; unknown cities stay unknown.
5. `IssueClassifier.classify`:
   - Bengaluru → optional model path, or a safe “model unavailable” review outcome;
   - Gurugram/unknown → deterministic keyword rules or clarification.
6. `PriorityEngine` computes severity, urgency, exposure, crowd and age components.
7. Duplicate detection compares nearby open issues.
8. `resolve_department` applies the city-scoped directory rules.
9. The issue, history and audit-relevant fields are persisted.
10. The API returns the ticket, category, priority, routing status and explainability
    fields.

No step invents a category, priority, department or coordinate.

---

## 5. Classification design

### Bengaluru

- Optional artifact: `backend/app/ml_models/india_bengaluru_v2_2_candidate/classifier.joblib`.
- Pipeline: TF-IDF + logistic regression exposing `predict_proba`.
- Accepted labels: seven mapped categories.
- Guards: minimum top score and minimum top-two margin.
- Failure/abstention: `OTHER`, `needs_clarification`, `needs_review=True`.
- When the artifact is absent, no model version is recorded and the reason says the
  artifact is unavailable.

### Gurugram and unsupported cities

- Deterministic keyword scoring with phrase weighting and light spelling normalisation.
- Clear winner → `india_keyword_rule`.
- Ambiguous text → `needs_clarification`.
- The Bengaluru model is never invoked.

---

## 6. Priority design

`PriorityEngine` is rule-based and explainable:

- category severity weight;
- urgency phrases (danger, children, hospital, live wire, flooding, etc.);
- exposure phrases (school, market, main road, junction, crowded area);
- duplicate/crowd signal;
- age of an open complaint.

The result includes a score, level and reasons. It is not a learned model and is not
presented as a calibrated probability.

---

## 7. Routing design

`resolve_department` is the only routing authority:

- exact city match required;
- active row required;
- `automatic` requires verification URL and date;
- `manual_review` leaves the issue unassigned with a reason;
- `disabled` is explicitly unassigned;
- no cross-city or category-only fallback.

This prevents a Gurugram complaint from reaching a Bengaluru department and prevents an
unverified mapping from silently assigning an officer.

---

## 8. Authentication and RBAC

- Passwords are hashed with bcrypt (native `bcrypt` package).
- JWTs are signed with `SECRET_KEY` using HS256.
- Role dependencies enforce `CITIZEN`, `OFFICER` and `ADMIN` at router level.
- Citizen self-registration is public; officer and administrator registration is not.
- Officer and administrator accounts are provisioned by local scripts with hidden
  password prompts and forced roles.
- Production startup rejects a missing, default or short `SECRET_KEY`.
- CORS rejects wildcard origins when credentials are enabled.

---

## 9. Frontend

- Static HTML files under `frontend/`.
- `index_india_staging.html` is the India historical dashboard.
- `citizen_portal.html`, `officer_portal.html` and `admin_portal.html` are the role
  portals.
- `index.html` is the legacy dashboard selected only by `UIMRS_FRONTEND_MODE=legacy`.
- Rendering uses `textContent`/`createTextNode` for untrusted values, not HTML injection.
- Leaflet and OpenStreetMap tiles are loaded from public CDNs.
- With an empty database, the dashboard shows explicit empty-state messages and does not
  draw fabricated charts.

---

## 10. Migrations and schema safety

Alembic revisions:

| Revision | Purpose |
|---|---|
| `0001_baseline` | Core tables |
| `0002_ai_explainability` | Classification explainability columns |
| `0003_department_routing` | Directory metadata and issue routing outcome |
| `0004_admin_audit_log` | Immutable administrative audit table |

`scripts/migrate_database.py` calls `prepare_database`, which:

- creates a new empty database through the full revision chain;
- stamps a recognised pre-Alembic database without recreating tables;
- upgrades an already versioned database;
- refuses an inconsistent or partially applied schema.

Startup never calls `Base.metadata.create_all()` on an existing database.

---

## 11. Empty-database behaviour

The public release intentionally starts empty:

- no historical complaints;
- no live complaints;
- no citizen/officer/administrator accounts;
- only the system service user and the seeded department directory.

The dashboard therefore shows zeros, `Not available` metrics, empty tables, no bars and
no hotspots, each with an explicit explanation. This is a truthful empty state, not a
failure.

---

## 12. Testing strategy

The `tests/` suite uses isolated temporary or in-memory SQLite databases. It covers:

- authentication and RBAC;
- citizen, officer and administrator workflows;
- issue read security and evidence access;
- city-aware classification and routing;
- explainability persistence;
- deployment readiness, backup and restore;
- frontend mode and dashboard truthfulness;
- bootstrap safety (no fabricated departments/complaints);
- configuration and secret-key safety.

Model-dependent tests are skipped with a clear reason when the public release does not
include the Bengaluru artifact. No test is deleted to hide a failure.
