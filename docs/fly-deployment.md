# Backend deployment — Fly.io + Neon + Nhost

Supersedes `phase-4-deployment.md` (Render). Stack:

| Concern | Before | Now |
|---|---|---|
| Compute | Render Web Service (Docker) | **Fly.io** machine (`bom`, Docker) |
| Database | Supabase Postgres (pooler) | **Neon** serverless Postgres |
| File storage | Supabase Storage | **Nhost Storage** (see `nhost-storage.md`) |

Why Fly over Render: the job worker is an in-process thread, and Render's free
service spins down after 15 min idle (killing mid-run jobs). Fly keeps one
machine always-on (`min_machines_running = 1`, `auto_stop_machines = false`).

Why Neon over the Nhost DB: the Nhost Postgres is shared with Hasura/Auth/
Storage and its free tier has no IP allowlist, so pointing the app at it means
opening that DB to the internet. Neon is a dedicated instance with a real free
tier.

---

## 1. Neon

1. Create a project at neon.tech (region: closest to `ap-south-1` — e.g.
   AWS `ap-south-1` Mumbai or `ap-southeast-1` Singapore).
2. Copy the connection string. Use the **pooled** one
   (`...-pooler.<region>.aws.neon.tech`) — it ends with `?sslmode=require`.
3. That string is `DATABASE_URL`. It's a secret — it never goes in `fly.toml`
   or git, only into `fly secrets`.

`backend/database.py` now sets `pool_pre_ping=True` + `pool_recycle=300` so the
app survives Neon closing idle connections / scaling to zero.

Schema is created on boot: the Dockerfile runs `alembic upgrade head` before
uvicorn. `SKIP_DB_AUTOCREATE=true` (set in `fly.toml`) stops the app trying to
`CREATE DATABASE`, which Neon doesn't allow.

**Data:** starting fresh. The old Supabase rows point at Supabase storage URLs
that are going away anyway. To carry data over instead:
`pg_dump "<supabase pooler url>" --no-owner --no-privileges -Fc -f dump.pgc`
then `pg_restore -d "<neon url>" dump.pgc` (expect the illustration/PDF URLs in
those rows to 404 until re-generated).

---

## 2. Fly.io

Install flyctl and log in (do this yourself — I can't run authenticated CLIs):

```
# in the Claude Code prompt you can run:  ! fly auth login
fly auth login
```

From the repo root (`fly.toml` is already committed):

```
fly apps create booktures            # pick another name if taken; update app= in fly.toml
```

Set secrets (values from your current Render env + the new Neon/Nhost ones):

```
fly secrets set \
  DATABASE_URL="postgresql://...neon.tech/...?sslmode=require" \
  APP_API_KEY="<same value the frontend sends as VITE_API_KEY>" \
  NHOST_ADMIN_SECRET="<Nhost console → project → Hasura → Admin Secret>" \
  GROQ_API_KEY="<...>" \
  GEMINI_API_KEY="<...>" \
  CF_ACCOUNT_ID="<...>" \
  CF_API_TOKEN="<...>"
```

Check `fly.toml` `[env]` — the `LLM_PROVIDER` / `IMAGE_PROVIDER` / model values
there must match what you run on Render today. Fix `CORS_ORIGINS` to the real
frontend origin.

`GEMINI_API_KEY` is also used for character extraction: `fly.toml` sets
`CHARACTER_EXTRACTION_MODE=llm` + `CHARACTER_LLM_PROVIDER=gemini` so the whole book
is profiled in one large-context pass. Without the key it falls back to
`LLM_PROVIDER` (Groq, ~8k context, spread-sampled).

Deploy:

```
fly deploy
```

Verify:

```
curl https://booktures.fly.dev/health          # {"status":"ok"}
curl https://booktures.fly.dev/ready            # {"status":"ready"} — real Neon round-trip
curl https://booktures.fly.dev/books            # 401 without the key
curl -H "X-API-Key: <APP_API_KEY>" https://booktures.fly.dev/books   # 200
```

---

## 3. Frontend

- `VITE_API_BASE_URL` → `https://booktures.fly.dev`
- `VITE_API_KEY` → same as the `APP_API_KEY` secret
- Then set `CORS_ORIGINS` on Fly to the frontend's origin and `fly deploy` again.

---

## 4. Decommission

Once Fly is verified serving traffic and a test upload works end to end
(PDF → analyze → generate images → illustrations load from Nhost):

- Render: delete the `booktures` web service.
- Supabase: delete the project (DB + storage). Point of no return — keep a
  `pg_dump` if unsure.

---

## Operational notes

- **One machine only.** A second instance = a second job worker =
  double-processed jobs, and `_reset_orphaned_jobs` on each boot requeues jobs
  the other machine is running. Don't `fly scale count 2`.
- **Deploys interrupt running jobs.** A rolling deploy stops the machine; any
  job mid-run is requeued on the new machine and re-run from the start
  (see `bug-audit-2026-09.md` #1). Deploy when the queue is idle.
- **OCR.** Tesseract renders pages at `OCR_DPI` (200 on Fly, down from 300) to
  keep the page bitmap small. LLM OCR-repair is capped at `OCR_REPAIR_MAX_CALLS`
  (40) per run so a long scanned book can't exhaust Groq's rate limit.
- **Memory.** 512 MB. spaCy's `en_core_web_sm` + pdfplumber on a large scanned
  PDF is the pressure point; bump `[[vm]] memory` to `1gb` if you see OOM
  restarts in `fly logs`.
- **Cost.** One always-on `shared-cpu-1x:512mb` is a few dollars/month of Fly
  usage — not truly free, but small. `fly dashboard` shows the meter.
