# Task 15 — Test Report

## Environment

- Interpreter: `backend/venv/Scripts/python.exe` (Python 3.13).
- Database used by tests: in-memory SQLite via `StaticPool` (no project database
  touched).
- Full command: `backend/venv/Scripts/python.exe -m pytest tests -q --disable-warnings`.

## Result

**490 passed, 0 failed** (64.18 s).

Breakdown:

- Pre-existing suite: 462 passed.
- New Task 15 tests: 28 passed (23 unified-site + 5 database-backed image storage).

## New tests added

`tests/test_task15_unified_site.py` covers:

- Unified routes: `/` serves `uimrs.html`; `/dashboard` serves the historical
  page; `/analytics` and `/gallery` serve their pages; portal routes unchanged;
  static assets and vendored Three.js served; favicon returns 204.
- Landing page: required navigation entries; exactly three future-development
  cards with the approved titles and badges; no dead/external links; scope and
  accuracy disclaimers.
- Analytics page: public historical vs restricted live sections separated;
  real endpoints and role gating; filters and honest priority note; no external
  assets.
- Gallery page: all nine categories; local Three.js only; interaction and
  fallback requirements; illustrative disclaimer.
- Analytics date filters: historical window applied; live rows never included;
  invalid/reversed dates rejected (422); admin overview city/date filters;
  role protection retained.

## Browser verification

Performed against `http://127.0.0.1:8000` with the default development database:

- Landing, analytics, gallery, dashboard, citizen, officer and admin pages all
  returned HTTP 200.
- Console error count: 0 on every page checked.
- Gallery: WebGL available, all nine category buttons rendered, no runtime
  errors, deep link `#road` works.
- Navigation links resolve to served routes (no 404s observed).

## Protected-data verification

SHA-256 hashes after all work:

- `uimrs.db` — `48B8AA8A...296F` (unchanged)
- `uimrs_india_candidate.db` — `76EF6FCA...F58B` (unchanged)
- `uimrs_india_dev.db` — `24043561...FE84` (migrated to `0005_image_data_bytea`; backup saved)
- `test_environment/uimrs_test.db` — `2CB2FC2E...E53F` (unchanged)

## Known limitations

- No public cloud resource has been created. Deployment is prepared only.
- The public deployment database contains no historical Bengaluru rows by
  design; its historical analytics panel will show an empty state until a
  licensed subset is explicitly approved.
- Screenshot artifacts are not attached because the agent runtime cannot read
  image output; visual verification was done through the browser accessibility
  tree and console checks.
