# Phase 4 — backend deployment (Render)

> **Superseded.** The stack moved off Render + Supabase to **Fly.io + Neon + Nhost**.
> See `fly-deployment.md` for the current runbook. Kept here as history.

## What changed

- Root `Dockerfile` — builds an API-only image from `backend/requirements-api.txt`
  (no torch/diffusers), installs `tesseract-ocr` for OCR fallback, runs
  `alembic upgrade head` then `uvicorn` on `$PORT` (Render sets this env var
  itself at runtime).
- Root `.dockerignore` — excludes `frontend/`, `docs/`, `backend/storage/`,
  `backend/.env`, and the GPU requirements file from the build context.

## Verified locally

Built the image and ran it in a real container against the actual Supabase
DB and Cloudflare/Groq keys:

- `docker build` succeeds off `requirements-api.txt` alone.
- Container boots, runs the Alembic migration, and starts uvicorn cleanly.
- `/health` → 200, `/ready` → `{"status": "ready"}` (real DB round-trip),
  `/books` → 401 without the API key, 200 with it.

## Bug caught: Supabase direct connection needs IPv6

The direct `DATABASE_URL` (`db.<ref>.supabase.co:5432`) resolves to an
IPv6-only address. Docker Desktop's default network - and most free-tier
hosts - have no IPv6 egress, so every connection failed with "Network is
unreachable". Fixed by switching to Supabase's **Session pooler** connection
string instead (`aws-0-<region>.pooler.supabase.com:5432`), which is
IPv4-compatible. Anyone deploying this needs the pooler string, not the
direct one - the direct string is fine for local dev only.

## Why Render instead of Hugging Face Spaces

Docker Spaces on HF require a payment method on file even for free CPU
Basic hardware, and the Docker SDK option stayed locked even after adding
a card. Render's Docker-based web services don't have that gate.

## Deploying to Render

1. Create a new **Web Service** at render.com, connect the GitHub repo
   (`pratikverse/booktures`), branch `main`.
2. Environment: **Docker** (Render auto-detects the root `Dockerfile`).
3. Instance type: **Free**.
4. Add environment variables (Render dashboard → Environment) with every
   value from `backend/.env.example` that has a real value in your local
   `backend/.env`: `DATABASE_URL` (the **pooler** string), `APP_API_KEY`,
   `STORAGE_PROVIDER`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
   `SUPABASE_BUCKET`, `LLM_PROVIDER`, `GROQ_API_KEY`, `GEMINI_API_KEY`,
   `IMAGE_PROVIDER`, `CF_ACCOUNT_ID`, `CF_API_TOKEN`,
   `SKIP_DB_AUTOCREATE=true`, and `CORS_ORIGINS` set to wherever the
   frontend ends up hosted. Render sets `PORT` itself - don't override it.
5. Deploy. Once live, confirm `https://<your-service>.onrender.com/health`
   and `/ready` both return healthy, then point the frontend's
   `VITE_API_BASE_URL` at that URL.

Note: Render's free web services spin down after 15 minutes of inactivity
and take ~30-60s to cold-start on the next request.

## Live

Deployed at https://booktures.onrender.com. Verified against the real
service: `/health` → `{"status":"ok"}`, `/ready` → `{"status":"ready"}`
(real Supabase round-trip), `/books` → 401 without the API key.

## Not done yet

Frontend hosting (Vercel/Netlify/Cloudflare Pages) - next step once decided.
Once it's up, update `CORS_ORIGINS` on Render to that origin and set the
frontend's `VITE_API_BASE_URL` to `https://booktures.onrender.com`.
