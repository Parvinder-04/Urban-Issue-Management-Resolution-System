# UIMRS — Intelligent Urban Issue Management & Resolution System

UIMRS is an academic, AI-assisted civic issue reporting and resolution platform for
Indian cities. It gives citizens a structured way to report local problems, gives
municipal officers a prioritised queue, and gives administrators an audit-friendly
oversight view. It was built as a major academic project and is published here as a
public, runnable distribution **without any private complaint records**.

> **Public-release scope.** This repository contains the working application source,
> the four web portals, the Alembic schema, runtime scripts and documentation. It does
> **not** contain the historical complaint database, the trained Bengaluru classifier
> artifact, test accounts, uploads, backups or internal reports. The application starts
> with an empty database and shows honest empty states. See
> [`docs/MODEL_AND_DATA_LICENSING.md`](docs/MODEL_AND_DATA_LICENSING.md).

---

## 1. Project title and purpose

**UIMRS — Intelligent Urban Issue Management & Resolution System.**

The purpose is to reduce the manual effort involved in receiving, reading, categorising,
prioritising, routing and tracking civic complaints. The system demonstrates an
end-to-end workflow:

1. a citizen submits a complaint with a location and optional evidence;
2. the backend classifies the complaint and estimates a priority;
3. nearby duplicate reports are linked;
4. a verified city department is assigned automatically only when the directory entry
   is safe to auto-route, otherwise the complaint goes to manual review;
5. an officer works the queue and records status updates;
6. an administrator reviews operations, accounts and the immutable audit log;
7. the public dashboard presents historical aggregate data when an authorised dataset
   has been configured.

## 2. Problem statement

Civic issues such as potholes, overflowing bins, broken streetlights, water leakage,
blocked drains and faulty traffic signals are usually reported through fragmented
channels. Manual triage is slow, urgent and non-urgent complaints sit in the same queue,
the same physical problem is reported many times, and authorities have little visibility
into recurring local patterns.

UIMRS addresses these problems with:

- deterministic, explainable complaint categorisation and priority scoring;
- conservative city-aware department routing;
- duplicate and hotspot clustering over real coordinates;
- role-separated citizen, officer and administrator workflows;
- an audit trail for administrative actions;
- honest analytics that do not invent resolution times or priority values.

## 3. Key features

### Implemented in this release

- FastAPI backend with versioned REST endpoints under `/api/v1`.
- JWT authentication, bcrypt password hashing and role-based access control
  (`CITIZEN`, `OFFICER`, `ADMIN`).
- Four browser portals served by the same application:
  - `/` — public historical dashboard (read-only);
  - `/citizen` — citizen registration, reporting, tracking and evidence upload;
  - `/officer` — officer queue, issue detail and status workflow;
  - `/admin` — administrator overview, review queue, officer management, department
    directory and audit log.
- City-aware classification:
  - Bengaluru: optional authorised model pipeline with conservative abstention;
  - Gurugram and unsupported cities: deterministic Indian keyword rules only.
- Explainable priority scoring (severity, urgency, exposure, crowd signal, age).
- Duplicate detection with haversine distance and DBSCAN clustering.
- Geographic hotspot analytics from recorded coordinates.
- Verified starter department directory for Bengaluru and Gurugram, seeded from public
  agency information by a separate, idempotent script.
- Alembic-managed SQLite schema, startup schema validation and `/ready` checks.
- Authenticated evidence uploads with image quality checks (blur, brightness,
  resolution) — not image-content classification.
- Backup and restore utilities with SHA-256 manifests.
- Empty-state dashboard messages when no historical data is configured.

### Deliberately not included

- Historical complaint rows, complaint free text, source record identifiers, or any
  private dataset export.
- The trained Bengaluru classifier binary and its training metadata.
- Real user accounts, password hashes, JWTs, secrets or `.env` values.
- Uploaded evidence, backups, logs, virtual environments or local databases.
- Any claim of pan-India machine-learning coverage.
- Any invented screenshots, team member names, metrics or resolution times.

### Future scope (not implemented here)

- Native mobile applications.
- Deep-learning image-content verification (for example, confirming that a photo really
  shows a pothole).
- Real-time push notifications and WebSocket live updates at city scale.
- Multi-city ML models with documented, licensed training data.
- Production deployment hardening (PostgreSQL, reverse proxy, external identity
  provider, centralised logging).
- Integration with municipal grievance APIs.

## 4. System architecture

```
Browser portals (HTML/CSS/JavaScript)
  /            historical dashboard
  /citizen     citizen portal
  /officer     officer portal
  /admin       administrator portal
        |
        | HTTPS / JSON (JWT bearer tokens)
        v
FastAPI application (backend/app)
  routers/        auth, issues, analytics, chatbot, officer, admin
  services/       classification, priority, routing, clustering,
                  image verification, evidence storage, workflows, audit
  core/           configuration and frontend selection
  db_guard.py     schema and SQLite integrity validation
  db_migrate.py   controlled Alembic migration runner
  bootstrap.py    system service user only (no departments, no complaints)
        |
        v
SQLite database (local_data/uimrs_public.db by default)
  Alembic revisions 0001 -> 0004
        |
        +--> local_data/uploads/evidence (authorised evidence files only)
```

The frontend is intentionally dependency-light: static HTML pages with vanilla
JavaScript, served by FastAPI. The historical dashboard uses Leaflet and OpenStreetMap
tiles from public CDNs. There is no React, Flutter or separate frontend build step in
this release.

Key safety properties:

- The historical dashboard is read-only and is restricted to records marked
  `is_historical`. Live citizen submissions never appear in public analytics.
- Department routing never falls back across cities.
- Automatic assignment requires an active directory row, `routing_mode=automatic`, a
  verification source URL and a verification date.
- Startup refuses a corrupt database, a schema with missing tables/columns, or an
  unexpected revision. It never silently repairs a schema.
- The public setup script seeds only department directory rows, never complaints,
  citizens or officers.

## 5. Technologies used

| Layer | Technology |
|---|---|
| Language | Python 3.11–3.13 (verified on Python 3.13) |
| Web framework | FastAPI, Uvicorn |
| Data access | SQLAlchemy 2.x, Alembic |
| Database | SQLite for local/academic use |
| Authentication | `python-jose` JWT, `bcrypt` password hashing |
| Validation | Pydantic 2 |
| ML / data (optional path) | scikit-learn, joblib, NumPy, pandas |
| Image checks | OpenCV (`opencv-python`) |
| Frontend | HTML5, CSS, vanilla JavaScript, Leaflet + OpenStreetMap |
| Tests | `unittest`, FastAPI `TestClient`, `httpx` |

Third-party licence notes are recorded in
[`docs/MODEL_AND_DATA_LICENSING.md`](docs/MODEL_AND_DATA_LICENSING.md).

## 6. How AI/ML is used

UIMRS separates machine learning from deterministic rules so that the system remains
honest about what it can and cannot do.

### Bengaluru path (optional model)

The original academic build used a Bengaluru-specific text pipeline: TF-IDF features
plus logistic regression, trained on mapped complaint categories. The operational
classifier applies conservative guards:

- the top model score must meet a minimum threshold;
- the gap between the top two scores must meet a minimum margin;
- only the seven labels produced by that pipeline are accepted;
- anything else becomes `OTHER` with `needs_clarification` and `needs_review=True`.

**The trained artifact is not redistributed in this public release** because no explicit
redistribution or training-data permission has been established. When the artifact is
absent, the classifier reports that the model is unavailable and sends Bengaluru
complaints to human review. It does not substitute another model and does not invent a
model version or confidence.

An owner who has documented permission can install an authorised artifact at:

```
backend/app/ml_models/india_bengaluru_v2_2_candidate/classifier.joblib
```

See [`backend/app/ml_models/README.md`](backend/app/ml_models/README.md) and
[`docs/MODEL_AND_DATA_LICENSING.md`](docs/MODEL_AND_DATA_LICENSING.md).

### Gurugram and unsupported cities (rules only)

Gurugram complaints use deterministic Indian keyword rules with light spelling/variant
normalisation. The Bengaluru model is never invoked for Gurugram or an unknown city.
The resulting source is recorded as `india_keyword_rule`. Ambiguous text goes to
`needs_clarification`.

### Other intelligent components

- **Duplicate detection** uses haversine distance plus DBSCAN over recorded coordinates.
- **Hotspot detection** clusters historical coordinates; it does not claim that a
  geographic cluster is a proven duplicate incident.
- **Image verification** checks technical quality only (blur, brightness, resolution).
  It does not classify image content.
- **Citizen support chatbot** uses pre-approved rule-based intents. It is not an LLM and
  does not create complaints.

## 7. Priority estimation

Priority is deterministic and explainable. The priority engine combines:

- **severity** — the issue category and explicit danger language;
- **urgency** — words such as accident, children, hospital, live wire, flooding;
- **exposure** — schools, markets, main roads, junctions, crowded places;
- **crowd signal** — duplicate count;
- **age** — how long an open complaint has remained unresolved.

The output includes a numeric score, a level (`Low`, `Medium`, `High`, `Critical`) and
human-readable reasons. Historical source records do not supply priority values, so the
public dashboard does not display historical priority scores.

## 8. Automatic and manual department routing

The verified department directory stores, per city and category:

- agency name, short name and jurisdiction;
- official contact email/phone and contact purpose;
- verification source URL and verification date;
- `routing_mode`: `automatic`, `manual_review` or `disabled`;
- active flag.

Routing rules:

- The city must match exactly; there is no cross-city fallback.
- `automatic` assigns a department only when a verification URL and date are present.
- `manual_review` leaves `department_id` empty and records an explicit review reason.
- `disabled` produces an explicitly unassigned issue.
- Missing mappings produce `unassigned_no_mapping`; no department is invented.

The public seed script provides 21 starter rows:

- 10 Bengaluru rows with no contacts and `manual_review`;
- 11 Gurugram rows using official public contacts and source URLs, with a mixture of
  `automatic`, `manual_review` and `disabled` modes.

These are public agency directory entries, not complaint records. They can be reviewed
or replaced by the operator. See
[`docs/UIMRS_PUBLIC_SETUP_GUIDE.md`](docs/UIMRS_PUBLIC_SETUP_GUIDE.md).

## 9. Citizen, officer and admin workflows

### Citizen workflow

1. Open `/citizen` and register a citizen account (public self-registration).
2. Submit a complaint with title, description, city, latitude/longitude and optional
   evidence.
3. The backend classifies, scores priority, checks for duplicates and resolves routing.
4. The citizen receives a ticket number and can track status and history.
5. Evidence access is authenticated and scoped.

### Officer workflow

1. An administrator provisions an officer account against an existing active
   department. There is no public officer registration.
2. Open `/officer` and sign in.
3. Work the department queue, open issue details, review classification explanations
   and evidence, and record status changes.
4. Every status change is stored in the issue history.

### Administrator workflow

1. An administrator account is created by a local provisioning script; there is no
   public administrator registration.
2. Open `/admin` to review city/category aggregates, the review queue and issue details.
3. Manage officer accounts and department directory entries.
4. Read the immutable administrative audit log.
5. Administrative writes are recorded with actor, action, target and timestamp.

## 10. Supported city coverage and limitations

**Supported operational cities:** Bengaluru, Karnataka and Gurugram, Haryana.

- Bengaluru has the optional ML path. In this public release the artifact is absent, so
  Bengaluru complaints go to review until an authorised artifact is installed.
- Gurugram uses keyword rules only. This is intentional and is not described as ML.
- Unknown cities fall back to keyword rules or review; they are never silently treated
  as Bengaluru.
- The starter directory is not a claim of complete municipal jurisdiction coverage.
  Asset ownership in Indian cities often varies by road, sector and agency; the
  directory therefore keeps uncertain mappings in manual review.
- There is **no pan-India ML model** and no claim of national coverage.
- The dashboard is a historical-data viewer. With an empty database it displays explicit
  empty-state messages rather than fabricated charts.

## 11. Installation instructions

### Quick start (Windows 10/11)

Prerequisites:

- Windows 10 or 11;
- Python 3.11, 3.12 or 3.13 (Python 3.13 recommended; 3.14+ is not verified);
- an internet connection for the first dependency install.

From the extracted release folder:

```powershell
.\setup_public_environment.cmd
```

The setup script:

1. creates `backend\venv`;
2. installs the pinned dependencies;
3. creates `local_data\`;
4. applies the Alembic migrations to `local_data\uimrs_public.db`;
5. seeds the verified starter department directory;
6. generates a local `local_data\.secret_key` (never printed);
7. validates the database schema, revision and evidence directory.

Then create your own administrator account:

```powershell
backend\venv\Scripts\python.exe scripts\provision_public_admin.py --name "Your Name" --email "you@example.com"
```

Start the application:

```powershell
.\start_public_environment.cmd
```

The full guide, including manual setup, officer provisioning, authorised historical data
and troubleshooting, is
[`docs/UIMRS_PUBLIC_SETUP_GUIDE.md`](docs/UIMRS_PUBLIC_SETUP_GUIDE.md).

## 12. How to run all four portals

After starting the application, open:

| Portal | URL | Purpose |
|---|---|---|
| Historical dashboard | <http://127.0.0.1:8000/> | Public read-only historical view |
| Citizen portal | <http://127.0.0.1:8000/citizen> | Register, report and track complaints |
| Officer portal | <http://127.0.0.1:8000/officer> | Department queue and status workflow |
| Administrator portal | <http://127.0.0.1:8000/admin> | Oversight, review queue, accounts, audit log |

Supporting endpoints:

| Endpoint | URL |
|---|---|
| Interactive API docs | <http://127.0.0.1:8000/docs> |
| ReDoc | <http://127.0.0.1:8000/redoc> |
| Health | <http://127.0.0.1:8000/health> |
| Database readiness | <http://127.0.0.1:8000/ready> |

Citizen accounts are self-registered in the citizen portal. Officer and administrator
accounts must be provisioned locally; this is deliberate and is not weakened by the
public release.

## 13. API documentation

FastAPI generates interactive documentation at `/docs` and `/redoc`. The main endpoint
groups are:

| Group | Prefix | Examples |
|---|---|---|
| Authentication | `/api/v1/auth` | `POST /register`, `POST /login`, `GET /me` |
| Issues | `/api/v1/issues` | create, list, detail, history, status update, evidence upload/content |
| Analytics | `/api/v1/analytics` | historical dashboard aggregate, source-agency distribution, heatmap |
| Chatbot | `/api/v1/chatbot` | rule-based support message |
| Officer | `/api/v1/officer` | overview, department queue, issue detail, status action |
| Administrator | `/api/v1/admin` | overview, issues, review queue, officers, departments, audit logs |

All write operations enforce JWT authentication and role checks at the router level.
Public analytics are restricted to historical records. The API schema is the source of
truth; use `/docs` for the exact request and response models.

## 14. Dataset provenance and availability limitations

### Historical complaint data

The original academic project used a Bengaluru complaint log associated with
I Change My City / Janaagraha (2019–2022) for internal analysis and model experiments.
**No historical complaint rows, free text, source identifiers or derived exports are
included in this public release.** No redistribution permission has been established,
so the public application starts empty.

When no historical data is configured:

- `/api/v1/analytics/dashboard` returns zeros and empty breakdowns;
- the dashboard displays explicit empty-state messages;
- no charts, hotspots, resolution times or priority values are fabricated.

An owner who has documented permission for a dataset can import it into their own
deployment. The application does not ship an importer that points at private paths; the
operator is responsible for provenance, consent and lawful use. See
[`docs/MODEL_AND_DATA_LICENSING.md`](docs/MODEL_AND_DATA_LICENSING.md).

### Bengaluru model

The trained artifact is excluded. The application remains runnable. Bengaluru
complaints are sent to human review with a clear “model unavailable” reason. To install
an authorised model, follow
[`backend/app/ml_models/README.md`](backend/app/ml_models/README.md). Do not substitute
an unrelated model and do not claim performance that has not been measured on your data.

### Starter department directory

The 21 starter rows are public agency directory information (names, official public
contacts, jurisdiction notes and verification URLs) seeded by
`scripts/seed_public_departments.py`. They are not complaint records and contain no
personal mobile numbers. Operators should re-verify contacts before operational use.

## 15. Screenshots

Screenshots are intentionally **not** included in this release. The original project's
screenshots may contain account names, complaint text or local paths.

Add your own screenshots after running the public release with an empty database:

- `[Screenshot placeholder: public historical dashboard empty state]`
- `[Screenshot placeholder: citizen registration and complaint form]`
- `[Screenshot placeholder: officer queue]`
- `[Screenshot placeholder: administrator overview and audit log]`
- `[Screenshot placeholder: interactive API documentation]`

Do not publish screenshots that show real complaints, personal data, tokens or local
filesystem paths.

## 16. Known limitations and future scope

### Known limitations

- SQLite is used for local/academic deployment; it is not a city-scale database.
- The Bengaluru model is not included in this public release, so the ML path is inactive
  until an authorised artifact is installed.
- Gurugram uses keyword rules, not ML; accuracy depends on the rule vocabulary.
- The starter directory is a starting point, not a guarantee of current municipal
  jurisdiction or contact details.
- The chatbot is rule-based and does not create tickets.
- Image verification checks quality, not whether the image depicts the reported issue.
- The dashboard is a historical viewer; it is not a live city operations console.
- No email/SMS delivery, push notifications or external grievance-system integration is
  implemented.
- No production identity provider, rate limiting at the edge, or centralised logging is
  included.

### Future scope

- Add licensed, documented multi-city training data and model governance.
- Add content-aware image verification.
- Add PostgreSQL/PostGIS deployment guidance and horizontal scaling.
- Add municipal API integrations and notification channels.
- Add richer accessibility and multilingual UI coverage.
- Add reproducible evaluation notebooks and model cards for authorised artifacts.

## 17. Team contribution

Team member names and individual contribution percentages are intentionally left as
placeholders. The academic owner should fill these in before publication.

| Role | Name | Contribution |
|---|---|---|
| Project lead | `[Name placeholder]` | `[Contribution placeholder]` |
| Backend / API | `[Name placeholder]` | `[Contribution placeholder]` |
| Frontend / UX | `[Name placeholder]` | `[Contribution placeholder]` |
| AI / data | `[Name placeholder]` | `[Contribution placeholder]` |
| Testing / documentation | `[Name placeholder]` | `[Contribution placeholder]` |
| Supervisor / guide | `[Name placeholder]` | `[Contribution placeholder]` |

Do not invent names or contribution claims.

## 18. Academic-use disclaimer and data privacy

This project is provided for academic demonstration and evaluation. It is **not** a
production municipal service and must not be presented as one.

- Do not upload real personal data, complaint evidence or identifying information to a
  public demo.
- Operators are responsible for lawful basis, consent, retention and access control for
  any data they load.
- The public release contains no private complaint records and no real accounts.
- Historical data, if configured by an owner, must come from a source with documented
  permission. Historical records are read-only and must never be mixed with live
  submissions.
- Security-sensitive values (secret key, tokens, password hashes) must never be
  committed to Git or shared in screenshots.
- The software is provided without warranty. See the licence status below.

## License

This project is licensed under the MIT License.
See the [LICENSE](LICENSE) file for details.

The MIT License applies to the project code owned by the contributors.
Third-party components, datasets, and separately licensed materials remain
subject to their respective terms.

The Bengaluru historical dataset and trained classifier are not included
in this public release.

## Documentation

- [`docs/UIMRS_PUBLIC_SETUP_GUIDE.md`](docs/UIMRS_PUBLIC_SETUP_GUIDE.md) — complete
  first-time setup, operation and troubleshooting.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — accurate implementation architecture.
- [`docs/DEPLOYMENT_GUIDE.md`](docs/DEPLOYMENT_GUIDE.md) — deployment, backup and
  readiness checks.
- [`docs/MODEL_AND_DATA_LICENSING.md`](docs/MODEL_AND_DATA_LICENSING.md) — model,
  dataset and third-party licence review.
- [`backend/app/ml_models/README.md`](backend/app/ml_models/README.md) — how to install
  an authorised Bengaluru model.
