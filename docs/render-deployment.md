# Deployment — Render (free tier)

Both the API and the frontend run on Render's free tier. Same DB (Neon) and
storage (Nhost) as `fly-deployment.md` — only the compute host differs.
`render.yaml` at the repo root is a Blueprint defining both services.

## Tradeoffs vs Fly

| | Fly (`min_machines_running=1`) | Render free |
|---|---|---|
| Idle behaviour | always on | sleeps after 15 min with no inbound request |
| Cold start | none | ~30–60 s |
| Job mid-sleep | keeps running | requeued + restarted from scratch on next wake |
| Cost | ~$3/mo | $0 |

The in-process job worker restarts cleanly on every boot (`_reset_orphaned_jobs`
requeues anything that was "processing"), so a sleep only *delays* a job, never
corrupts it. Keep the app tab open while a book generates — the Jobs page polls
job status, and that inbound traffic keeps the service awake.

## Setup

1. Push `main` (the Blueprint reads from GitHub).
2. Render dashboard → **New → Blueprint** → connect `pratikverse/Booktures`.
   It picks up `render.yaml` at the repo root and creates two services:
   `booktures` (Docker API, free, Singapore) and `booktures-web` (Vite static
   site, free).
3. Fill the secrets Render prompts for (`sync: false` in the blueprint):

   | Service | Var | Value |
   |---|---|---|
   | booktures | `DATABASE_URL` | Neon pooled string (`...-pooler.<region>.aws.neon.tech/neondb?sslmode=require`) |
   | booktures | `APP_API_KEY` | a strong random string |
   | booktures | `NHOST_ADMIN_SECRET` | Nhost console → project → Settings → Hasura → Admin Secret |
   | booktures | `GROQ_API_KEY` | Groq console |
   | booktures | `GEMINI_API_KEY` | Google AI Studio |
   | booktures | `CF_ACCOUNT_ID` | Cloudflare dashboard |
   | booktures | `CF_API_TOKEN` | Cloudflare → Workers AI token |
   | booktures-web | `VITE_API_KEY` | **same value** as `APP_API_KEY` above |

   Everything else (providers, models, OCR tuning, Nhost URL/bucket,
   `CORS_ORIGINS`, `VITE_API_BASE_URL`) is baked into `render.yaml`.
   **Don't set `PORT`** — Render injects it.
4. Deploy. The API runs `alembic upgrade head` then uvicorn (Dockerfile CMD);
   the web service runs `npm ci && npm run build` and serves `frontend/dist`.
5. Verify:
   ```
   curl https://booktures.onrender.com/health          # {"status":"ok"}
   curl https://booktures.onrender.com/ready            # {"status":"ready"} — real Neon round-trip
   curl -H "X-API-Key: <APP_API_KEY>" https://booktures.onrender.com/books   # 200
   ```
   Then open `https://booktures-web.onrender.com` and upload a test PDF.

## If the frontend service gets a different name

`CORS_ORIGINS` in `render.yaml` hard-codes `https://booktures-web.onrender.com`.
If Render assigns a different hostname (name collision), update `CORS_ORIGINS`
on the `booktures` service to match and redeploy it.

## Operational notes

Same as `fly-deployment.md` §Operational notes, plus:

- **One instance only.** Free tier is single-instance anyway, but never scale
  up — a second instance means a second job worker double-processing jobs.
- **First request after a sleep is slow.** The frontend's health check will
  show the API as down for ~30–60 s while it cold-starts.
- **Blueprint edits.** Changing `render.yaml` and pushing updates the service
  config on the next deploy; secret values entered in the dashboard are kept.
