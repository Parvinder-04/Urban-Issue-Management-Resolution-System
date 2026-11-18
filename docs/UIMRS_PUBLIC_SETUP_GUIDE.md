# UIMRS Public Setup Guide

This guide takes a clean clone or ZIP extraction to a working local UIMRS
installation with an **empty database**, no historical complaint records, no real
accounts and no redistributed model artifact.

The public release is designed so that every path is derived from the project folder.
Nothing depends on the creator's laptop, user name, drive letter or private data.

---

## 1. Supported Python versions

| Version | Status |
|---|---|
| Python 3.11 | Supported |
| Python 3.12 | Supported |
| Python 3.13 | Recommended and verified |
| Python 3.14+ | Not verified; pinned scientific packages may lack wheels |
| Python 3.10 or older | Not supported |

Check your interpreter:

```powershell
py -3.13 --version
```

If `py` is unavailable, install Python 3.13 from
<https://www.python.org/downloads/> and tick **Add python.exe to PATH**.

---

## 2. Get the project

### From a Git clone

```powershell
git clone <PUBLIC_REPOSITORY_URL> UIMRS
cd UIMRS
```

Replace `<PUBLIC_REPOSITORY_URL>` with the URL of the public repository after the owner
publishes it. This release does not add a remote automatically.

### From a ZIP

Extract `UIMRS_Public_Release.zip` to a normal folder, for example:

```text
C:\Projects\UIMRS
```

Open PowerShell in that folder:

```powershell
cd C:\Projects\UIMRS
```

Do not run the application from inside the ZIP file.

---

## 3. First-time Windows setup (recommended)

The setup script is idempotent: re-running it reuses the virtual environment, database
and secret instead of overwriting them.

```powershell
.\setup_public_environment.cmd
```

If PowerShell blocks the script, the `.cmd` wrapper already applies a per-process
execution-policy bypass. You can also run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_public_environment.ps1
```

The script performs these steps:

1. locates Python 3.11–3.13;
2. creates `backend\venv`;
3. installs `backend\requirements.txt`;
4. creates `local_data\` and `local_data\uploads\evidence\`;
5. applies the Alembic migrations to `local_data\uimrs_public.db`;
6. seeds the 21-row verified starter department directory;
7. generates `local_data\.secret_key` with a cryptographically random value;
8. validates the schema, revision and evidence directory without starting the server.

The secret key is never printed. It is stored only in `local_data\.secret_key`, which is
Git-ignored.

### What setup does **not** do

- It does not copy a historical complaint database.
- It does not create citizens, officers or administrators.
- It does not create complaints, analytics or demo records.
- It does not download or install a classifier artifact.
- It does not weaken startup validation, authentication or RBAC.

---

## 4. Manual setup (alternative)

Use this if you prefer to run each step yourself.

### 4.1 Create the virtual environment

```powershell
py -3.13 -m venv backend\venv
backend\venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

### 4.2 Create local directories

```powershell
New-Item -ItemType Directory -Force local_data\uploads\evidence | Out-Null
```

### 4.3 Apply migrations to a new empty database

```powershell
backend\venv\Scripts\python.exe scripts\migrate_database.py `
  --database-url "sqlite:///C:/Projects/UIMRS/local_data/uimrs_public.db"
```

Use the absolute path of **your** folder. The command creates the SQLite file if it does
not exist and applies revisions `0001_baseline` through `0004_admin_audit_log`.

Check the revision without changing anything:

```powershell
backend\venv\Scripts\python.exe scripts\migrate_database.py `
  --database-url "sqlite:///C:/Projects/UIMRS/local_data/uimrs_public.db" --status
```

### 4.4 Seed the verified starter department directory

```powershell
backend\venv\Scripts\python.exe scripts\seed_public_departments.py `
  --database-url "sqlite:///C:/Projects/UIMRS/local_data/uimrs_public.db"
```

This inserts 21 public agency directory rows if they are missing. It never creates
complaints or users. Re-running it is safe and does not overwrite rows you have edited.

### 4.5 Generate a local secret key

```powershell
backend\venv\Scripts\python.exe -c "import secrets, pathlib; p=pathlib.Path('local_data/.secret_key'); p.write_text(secrets.token_urlsafe(48), encoding='ascii'); print('secret written to', p)"
```

Never commit `local_data/`. Never paste the secret into a document, screenshot or chat.

### 4.6 Validate before starting

```powershell
$env:DATABASE_URL = "sqlite:///C:/Projects/UIMRS/local_data/uimrs_public.db"
$env:UPLOAD_DIR = "C:\Projects\UIMRS\local_data\uploads"
$env:SECRET_KEY = (Get-Content local_data\.secret_key -Raw).Trim()
$env:UIMRS_FRONTEND_MODE = "india"
$env:PYTHONPATH = "C:\Projects\UIMRS\backend"
backend\venv\Scripts\python.exe scripts\verify_public_startup.py
```

---

## 5. Configure the application safely

The recommended path is to use the public launcher, which sets the environment for the
current process only.

If you configure variables yourself, use:

| Variable | Public default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///.../local_data/uimrs_public.db` | Absolute SQLite URL recommended |
| `UPLOAD_DIR` | `.../local_data/uploads` | Evidence storage |
| `SECRET_KEY` | generated file | At least 32 characters |
| `UIMRS_ENV` | `development` | `production` enforces strict secret rules |
| `UIMRS_FRONTEND_MODE` | `india` | Selects the India dashboard |
| `ALLOWED_ORIGINS` | localhost defaults | Explicit origins only; `*` is rejected |
| `MAX_UPLOAD_SIZE_MB` | `10` | Upload limit |

Do **not** put a real secret in `.env.example` or commit a `.env` file.

---

## 6. Provision the administrator securely

There is no public administrator registration. Create the first administrator locally:

```powershell
backend\venv\Scripts\python.exe scripts\provision_public_admin.py `
  --name "Your Name" `
  --email "you@example.com"
```

The script:

- requires a validated, current database;
- asks you to confirm the action;
- reads the password twice through hidden prompts;
- hashes it with bcrypt;
- forces the `ADMIN` role in code;
- refuses duplicate e-mail addresses and never modifies existing users;
- never prints the password or hash.

Use your own name and e-mail. Do not publish the account details.

---

## 7. Provision an officer securely

Officers must belong to an existing active department. The seed script provides the
starter directory.

```powershell
backend\venv\Scripts\python.exe scripts\provision_officer.py
```

The script lists active departments in supported cities, asks you to select one, then
prompts for the officer's name, e-mail, optional official phone and a password (hidden,
twice). It forces the `OFFICER` role and never creates departments.

If the list is empty, run the department seed script first:

```powershell
backend\venv\Scripts\python.exe scripts\seed_public_departments.py
```

---

## 8. Citizen self-registration

Citizens register themselves:

1. start the application (next section);
2. open <http://127.0.0.1:8000/citizen>;
3. choose **Register**;
4. use your own e-mail and a password you choose;
5. sign in and submit a complaint.

No citizen accounts are pre-created. Do not use real personal data in a public demo.

---

## 9. Start and stop the application

### Start

```powershell
.\start_public_environment.cmd
```

or:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_public_environment.ps1
```

The launcher:

- requires `backend\venv`;
- requires `local_data\uimrs_public.db`;
- reads the local secret from `local_data\.secret_key`;
- validates the schema and revision **without applying migrations**;
- starts Uvicorn on `127.0.0.1:8000`.

Keep the PowerShell window open while using the application.

### Stop

Press `Ctrl+C` in the PowerShell window, or close the window.

If port 8000 is already in use:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object OwningProcess
Stop-Process -Id <PID>
```

Only stop a process you own.

---

## 10. Portal URLs

| Portal | URL |
|---|---|
| Historical dashboard | <http://127.0.0.1:8000/> |
| Citizen portal | <http://127.0.0.1:8000/citizen> |
| Officer portal | <http://127.0.0.1:8000/officer> |
| Administrator portal | <http://127.0.0.1:8000/admin> |
| API documentation | <http://127.0.0.1:8000/docs> |
| ReDoc | <http://127.0.0.1:8000/redoc> |

The dashboard is public and read-only. The other portals require the appropriate role.

---

## 11. Health and readiness checks

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Expected results:

- `/health` returns `{"status":"healthy"}`.
- `/ready` returns `{"status":"ready","database":"ok","revision":"0004_admin_audit_log"}`
  when the database schema and revision are correct.
- `/ready` returns HTTP 503 if the database is missing, corrupt, has an invalid schema
  or is not at the expected revision.

The launcher runs the same validation before starting Uvicorn.

---

## 12. Expected empty state

With a new empty database and no historical dataset:

- the dashboard cards show `0` historical complaints and `Not available` for resolution
  metrics;
- the issue distribution area shows an explicit “no historical category data” message;
- the complaint tables show “no historical complaint records are available” rows;
- the source-agency table shows an explicit empty message;
- hotspot areas say that no geographic cluster meets the threshold;
- no charts, hotspots, resolution times or priority values are invented.

This is the correct public state. It is not a setup failure.

---

## 13. Configure authorised historical data (optional)

The public release does not include historical complaint records. If you have a dataset
with documented permission, you may load it into your own deployment.

Before loading data:

1. confirm your legal basis and permission to use the data;
2. confirm that personal data has been removed or is lawfully processed;
3. keep a copy of the provenance and permission record outside the repository;
4. never commit the database or exports to Git.

The application expects historical rows to be marked `is_historical = 1` and to carry a
city, source record identifier, category and coordinates where available. Public
analytics only read historical rows, so live submissions never leak into the public
dashboard.

The original academic project used a private importer for a Bengaluru complaint log.
That importer and the private dataset are **not** part of this release. The operator must
supply an approved import process and verify the resulting schema and counts. Do not
fabricate rows to make the dashboard look populated.

### Authorised Bengaluru model (optional)

The trained classifier is also excluded. If you have documented permission to use an
authorised artifact, place it at:

```text
backend/app/ml_models/india_bengaluru_v2_2_candidate/classifier.joblib
```

and, if you have an approved metadata file, at:

```text
backend/app/ml_models/india_bengaluru_v2_2_candidate/metadata.json
```

The pipeline must expose `predict_proba` and the seven expected labels. Follow
[`backend/app/ml_models/README.md`](../backend/app/ml_models/README.md). The application
does not download models automatically.

---

## 14. Running the tests

```powershell
backend\venv\Scripts\python.exe -B -m unittest discover -s tests
```

Tests use isolated temporary databases and do not modify `local_data\uimrs_public.db`.
Model-dependent tests are skipped with a clear reason when the classifier artifact is
absent. This is intentional: the public release does not hide a missing model.

A focused deployment check:

```powershell
backend\venv\Scripts\python.exe -B -m unittest tests.test_deployment_readiness -v
```

---

## 15. Backup and restore

Create a backup (database plus evidence plus SHA-256 manifest):

```powershell
$env:DATABASE_URL = "sqlite:///C:/Projects/UIMRS/local_data/uimrs_public.db"
$env:UPLOAD_DIR = "C:\Projects\UIMRS\local_data\uploads"
backend\venv\Scripts\python.exe scripts\backup_uimrs.py
```

Restore into a **new** directory; the utility never overwrites a live database:

```powershell
backend\venv\Scripts\python.exe scripts\restore_uimrs.py --backup-dir <BACKUP_DIR> --target-dir <NEW_DIR>
```

Read the command help for the exact options:

```powershell
backend\venv\Scripts\python.exe scripts\backup_uimrs.py --help
backend\venv\Scripts\python.exe scripts\restore_uimrs.py --help
```

Backups contain whatever data you loaded. Treat them as sensitive.

---

## 16. Troubleshooting

### `Python 3.11 to 3.13 is required`

Install Python 3.13 and re-run setup. Check `py -0p` to see installed versions.

### `backend\venv exists but is incomplete`

Delete only `backend\venv` and run setup again. Do not delete `local_data` unless you
intend to reset the database.

### `Dependency installation failed`

Check the network connection and the Python version. The pins are verified on Python
3.13. Do not mix packages from a different Python installation.

### `database revision ... does not match`

Apply the migrations:

```powershell
backend\venv\Scripts\python.exe scripts\migrate_database.py
```

Then check `/ready` again.

### `/ready` returns 503

Possible causes:

- `DATABASE_URL` points at a missing or different file;
- the schema is incomplete;
- the database is corrupt;
- the revision is not `0004_admin_audit_log`.

Run:

```powershell
backend\venv\Scripts\python.exe scripts\migrate_database.py --status
```

### `No active departments exist in a supported city`

Run the seed script:

```powershell
backend\venv\Scripts\python.exe scripts\seed_public_departments.py
```

### Administrator provisioning refuses

The script requires a validated database at the current revision. Run migrations and
check that `DATABASE_URL` points at `local_data\uimrs_public.db`, not at a legacy or
private database.

### Port 8000 is already in use

Stop the process that owns the port, or start on another port:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_public_environment.ps1 -Port 8001
```

Then use `http://127.0.0.1:8001/`.

### The dashboard looks empty

That is expected with a new database. The public release ships no historical records.
See section 12.

### Bengaluru complaints go to manual review

The Bengaluru model artifact is not redistributed. This is expected. Install an
authorised artifact only if you have documented permission. See section 13.

### I want to reset my local database

Stop the server, then delete only the local database and recreate it:

```powershell
Remove-Item local_data\uimrs_public.db
backend\venv\Scripts\python.exe scripts\migrate_database.py
backend\venv\Scripts\python.exe scripts\seed_public_departments.py
```

This deletes your local complaints and accounts. It does not touch any other database.

---

## 17. Data-safety rules

- Never commit `local_data/`, `.env`, databases, uploads, backups or logs.
- Never publish real complaint text, evidence, e-mail addresses or phone numbers.
- Never publish the secret key or a JWT.
- Do not use the public demo for real municipal complaints.
- Do not claim that the public release contains the original historical dataset or the
  trained model; it does not.
