# Backend deployment — Render (free tier)

Fallback host for when Fly.io's payment gate isn't worth it. Same image, same
DB (Neon), same storage (Nhost) — only the compute host differs from
`fly-deployment.md`.

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
   It picks up `render.yaml` at the repo root and creates the `booktures`
   web service (Docker, free, Singapore).
3. Open the service → **Environment** and fill the secrets (`sync: false` in
   the blueprint, so Render prompts for them):

   | Var | Value |
   |---|---|
   | `DATABASE_URL` | Neon pooled string (`...-pooler.<region>.aws.neon.tech/neondb?sslmode=require`) |
   | `APP_API_KEY` | a strong random string — the frontend sends the same value as `VITE_API_KEY` |
   | `NHOST_ADMIN_SECRET` | Nhost console → project → Settings → Hasura → Admin Secret |
   | `GROQ_API_KEY` | Groq console |
   | `GEMINI_API_KEY` | Google AI Studio |
   | `CF_ACCOUNT_ID` | Cloudflare dashboard |
   | `CF_API_TOKEN` | Cloudflare → Workers AI token |
   | `CORS_ORIGINS` | the deployed frontend origin, e.g. `https://booktures.pages.dev` |

   Everything else (providers, models, OCR tuning, Nhost URL/bucket) is baked
   into `render.yaml`. **Don't set `PORT`** — Render injects it.
4. Deploy. Render runs `alembic upgrade head` then uvicorn (the Dockerfile CMD).
5. Verify:
   ```
   curl https://booktures.onrender.com/health          # {"status":"ok"}
   curl https://booktures.onrender.com/ready            # {"status":"ready"} — real Neon round-trip
   curl -H "X-API-Key: <APP_API_KEY>" https://booktures.onrender.com/books   # 200
   ```

## Frontend

- `VITE_API_BASE_URL` → `https://booktures.onrender.com`
- `VITE_API_KEY` → same as `APP_API_KEY`
- Then set `CORS_ORIGINS` on Render to the frontend origin and redeploy.

## Operational notes

Same as `fly-deployment.md` §Operational notes, plus:

- **One instance only.** Free tier is single-instance anyway, but never scale
  up — a second instance means a second job worker double-processing jobs.
- **First request after a sleep is slow.** The frontend's health check will
  show the API as down for ~30–60 s while it cold-starts.
- **Blueprint edits.** Changing `render.yaml` and pushing updates the service
  config on the next deploy; secret values entered in the dashboard are kept.
