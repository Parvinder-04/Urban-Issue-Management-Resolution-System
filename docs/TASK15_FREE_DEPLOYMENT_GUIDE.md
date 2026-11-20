# Task 15 — Free-Tier Deployment Architecture

## Decision

No paid resource will be created. The public deployment uses only permanent free tiers
with no credit card and no trial credits.

## Recommended free architecture (lowest risk)

| Layer | Provider / plan | Why | Free limit relevant to UIMRS |
|---|---|---|---|
| Frontend + backend (same origin) | Render Free Web Service | No card, 750 instance-hours/month, sleeps after 15 min idle | One service running 24/7 uses ~720 h/month |
| Database | Neon Free Postgres | No card, permanent free tier, auto-wakes from suspend | 1 GB storage, 100 CU-hours/month, auto-suspend after 5 min |
| Image evidence | Database-backed (BYTEA) **or** Cloudinary Free | No card; keeps authorisation simple | 1 GB total DB size (BYTEA) or 25 Cloudinary credits |
| Keep-alive | UptimeRobot Free | No card, 5-min pings | 50 monitors |
| Optional static frontend | Cloudflare Pages Free | No card, global CDN | 500 builds/month |

### Why same origin on Render

The portals use a relative `/api/v1` prefix. Keeping the frontend and API on the
same Render service avoids CORS and API-base configuration and reduces failure
points. Cloudflare Pages can still be used later as an optional static mirror.

### Why Neon instead of Supabase

Supabase Free projects pause after 7 days of inactivity and require a manual
resume. Neon suspends compute after 5 minutes but wakes automatically on the
next request, and the Free plan needs no card. Neon is therefore safer for an
evaluation day.

### Why database-backed images (recommended) or Cloudinary

Render Free has no persistent disk. To satisfy "images must not disappear after
a restart" without buying storage, evidence bytes are stored in Neon as
`BYTEA` (recommended) or uploaded to Cloudinary Free. Database-backed storage
preserves the existing authenticated download endpoint and avoids a third-party
account; Cloudinary offloads the 1 GB database limit.

## Sleep and cold-start plan

- `/health` must not touch the database (it already does not).
- UptimeRobot pings `/health` every 5 minutes, keeping Render awake.
- Neon sleeps after 5 minutes; the first database request after sleep wakes it
  in 1-2 seconds.
- `/ready` performs the full database check and is used for manual verification.

## Required code/configuration changes

1. Add `psycopg2-binary` (or `psycopg[binary]`) to a free-deployment
   requirements file.
2. Make `guard_startup`/`database.py` continue to treat non-SQLite URLs as
   non-file databases (already the case).
3. Make `scripts/build_public_database.py` DB-agnostic: skip SQLite-only
   destination validation for Postgres URLs and skip SQLite backups.
4. (If database-backed images are chosen) add an `image_data BYTEA` column to
   `issue_images`, an Alembic revision, and update the upload/content endpoints.
   This is a schema change and needs approval before implementation.
5. Optional: make the frontend API base configurable for a Cloudflare Pages
   mirror (`window.UIMRS_API_BASE`).

## Migration and backup plan

- Schema: run Alembic `upgrade head` against the Neon URL
  (`DATABASE_URL=postgresql://...`).
- Seed: run the generic initializer to create the system user, 11 verified
  Gurugram departments and the administrator account.
- No historical Bengaluru rows are copied.
- Backups: use Neon branching or `pg_dump`; the SQLite backup helper does not
  apply to Postgres.
- Restore: restore the Neon branch/dump and restart Render.

## Step-by-step deployment (after approval)

1. Create a Neon Free project and copy its connection string.
2. Create a Cloudinary Free account (only if not using database-backed images)
   and copy `CLOUDINARY_URL`.
3. Create a Render Free Web Service from the repository using
   `deploy/Dockerfile` and `deploy/start.sh`.
4. Set environment variables:
   - `UIMRS_ENV=production`
   - `UIMRS_FRONTEND_MODE=india`
   - `SECRET_KEY=<generated, >=32 chars>`
   - `DATABASE_URL=<Neon connection string>`
   - `UPLOAD_DIR=/tmp/uimrs-uploads` (only if local fallback is used)
   - `UIMRS_SEED_ADMIN_EMAIL`, `UIMRS_SEED_ADMIN_PASSWORD`
   - Optional: `CLOUDINARY_URL`
5. Wait for the first build; verify `/ready` returns 200.
6. Create an UptimeRobot monitor for `https://<app>.onrender.com/health`.
7. Sign in as administrator and provision officers.
8. Optionally create one `[EVALUATION TEST]` complaint.

## Free-tier limitations and evaluation-day risks

- Render Free sleeps after 15 minutes without inbound traffic; UptimeRobot keeps
  it awake, but a missed ping can cause a ~1 minute cold start.
- Neon Free auto-suspends after 5 minutes; the first database query after
  suspend takes 1-2 seconds.
- Neon Free storage is 1 GB; database-backed images consume this budget.
- Render Free has no persistent disk; if database-backed or Cloudinary storage
  is not configured, uploads are lost on restart.
- Free plans can change; verify limits before the evaluation date.
- No custom domain or always-on guarantee is included.

## Image storage implementation (Option 1, completed)

Database-backed storage is implemented:

- Alembic revision `0005_image_data_bytea` adds `issue_images.image_data`
  (`BYTEA`/`BLOB`, nullable).
- Set `EVIDENCE_STORAGE_BACKEND=database` to store validated and compressed
  evidence bytes in PostgreSQL/SQLite instead of the filesystem.
- Images are resized to a 1600 px longest side and re-encoded (JPEG quality 85,
  WebP quality 85, PNG compression 9) before storage.
- The authenticated content endpoint serves bytes from the database with the
  correct media type; unauthorised callers still receive 401/404.
- Default remains `local` for development and existing tests.

### Migration details

- Revision: `0005_image_data_bytea` (down_revision `0004_admin_audit_log`).
- Operation: `ALTER TABLE issue_images ADD COLUMN image_data BYTEA/BLOB NULL`.
- No existing row is rewritten; `image_url` keeps its meaning for
  filesystem-backed rows.
- `backend/app/db_guard.py` now requires `image_data` in the migrated schema.

## What is not done yet

- No cloud resource has been created or connected.
- No billing or credit card has been used.
- Final online verification remains pending approval.
