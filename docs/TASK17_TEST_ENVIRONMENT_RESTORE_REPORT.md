# UIMRS — Task 17 Test Environment Restoration Report

## Status

**DONE**

The original test environment database was safely migrated to the current
application schema. All existing accounts, complaints, relationships and
historical records were preserved. All three portal login flows were verified
successfully.

## Root cause

`test_environment/uimrs_test.db` was at Alembic revision
`0004_admin_audit_log`. The application now requires revision
`0005_image_data_bytea`, which adds the nullable `image_data` column to
`issue_images`. The startup guard therefore reported:

`Database schema is incompatible: table 'issue_images' is missing column: image_data`

No data was lost and no fallback to the development database occurred.

## Backup

- Backup location:
  `backups/uimrs_test.pre_task17_20261120_154218.db`
- Backup integrity: `ok`
- Backup revision before migration: `0004_admin_audit_log`
- Backup users: 21
- Backup issues: 12057

## Database revision

- Before: `0004_admin_audit_log`
- After: `0005_image_data_bytea`

## Counts before and after

| Metric | Before | After |
|---|---:|---:|
| Users total | 21 | 21 |
| Administrators | 5 | 5 |
| Citizens | 3 | 3 |
| Officers | 13 | 13 |
| Issues total | 12057 | 12057 |
| Historical issues | 11969 | 11969 |
| Live issues | 88 | 88 |
| Issue images | 0 | 0 |
| Status updates | 91 | 91 |
| Audit logs | 50 | 50 |

## Files modified

No project source files were modified during Task 17.

The only modified project data file was:

- `test_environment/uimrs_test.db`

A verified backup was created at:

- `backups/uimrs_test.pre_task17_20261120_154218.db`

## Migration applied

- `0004_admin_audit_log` -> `0005_image_data_bytea`
- Operation: `ALTER TABLE issue_images ADD COLUMN image_data BYTEA/BLOB NULL`
- No existing rows were rewritten.
- `PRAGMA integrity_check` returned `ok` after migration.

## Verification

- `start_test_environment.cmd` started successfully.
- `GET /health` returned `{"status":"healthy"}`.
- `GET /ready` returned:
  - `status`: `ready`
  - `database`: `ok`
  - `revision`: `0005_image_data_bytea`
- Administrator login: verified successfully.
- Citizen login: verified successfully.
- Officer login: verified successfully.
- Existing complaint counts and historical/live separation preserved.
- Unified website routes and analytics remain compatible with the test data.

## Tests executed

- Relevant test suites previously passed:
  - `tests/test_secure_image_uploads.py`
  - `tests/test_task15_unified_site.py`
  - `tests/test_ai_explainability_persistence.py`
  - `tests/test_frontend_mode.py`
  - `tests/test_admin_portal.py`
- Full suite result available from Task 15 verification: **490 passed, 0 failed**.

## Remaining issues

None identified for the test environment restoration.

## Protected databases

The following databases were not modified:

- `uimrs.db`
- `uimrs_india_candidate.db`
- `uimrs_india_dev.db`

No classifier or model files were modified. No cloud resource was created and no
billing was connected.