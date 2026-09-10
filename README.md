# Booktures — PDF → Illustrated Book Pipeline

1. **Extraction pipeline** — per-page text is recovered from a PDF by running
   **multiple extractors** (PyMuPDF + pdfplumber in layout/plain/words modes),
   scoring each candidate with a hand-rolled readability heuristic, and falling
   back to **Tesseract OCR** (with a budgeted LLM repair pass) only when the
   digital text scores poorly. Running headers/footers are stripped by a
   frequency gate followed by an LLM classifier.
2. **Illustration pipeline** — an LLM builds a **"Visual Bible"** (one fixed
   physical description per character, extracted once for the whole book), then
   for each page a two-stage LLM pass produces a scene summary and an
   image-model prompt with the Visual Bible inlined, which a **text-to-image
   model** renders at a deterministic per-page seed.
3. **Job queue** — uploads create a `Job` row; a single in-process
   `GenerationWorker` daemon thread claims jobs from Postgres via
   `FOR UPDATE SKIP LOCKED`, with **pause / resume / cancel / retry** and
   **restart recovery** (orphaned `running` jobs are requeued on boot).

## ⚠️ Everything AI is behind a provider interface — nothing is hardcoded to a vendor

Three abstract interfaces (`LLMProvider`, `ImageProvider`, `StorageProvider`)
have **10 concrete implementations** between them. The pipeline code only ever
calls the interface; an env var picks the active backend, and the same code runs
on a local GPU or a $0/month 512 MB cloud container.

| Interface | Implementations | Local default | Cloud default |
|---|---|---|---|
| `LLMProvider` | Ollama, Groq, Gemini | Ollama | Groq (`openai/gpt-oss-20b`) |
| `ImageProvider` | diffusers, pollinations, workers-ai, gemini | diffusers (SDXL) | Cloudflare Workers AI (SDXL) |
| `StorageProvider` | local disk, Nhost, Cloudflare R2 | local disk | Nhost Storage |

Heavy dependencies (`torch`, `diffusers`) are imported lazily **inside** the
local provider, so the cloud image (`requirements-api.txt`) never installs them.

## Layout

```
backend/main.py                          FastAPI app + lifespan: init_db, load settings, start worker
backend/api/routes.py                    REST endpoints (books, jobs, settings, content) behind one X-API-Key gate
backend/services/pdf_service.py          multi-extractor text parsing, _extraction_score, OCR fallback, header/footer removal
backend/services/character_service.py    Visual Bible: single-pass LLM extraction + union-find alias grouping
backend/services/prompt_service.py       two-stage per-page: scene summary -> image prompt
backend/services/generation_queue_service.py  GenerationWorker daemon thread, orphan recovery, pause loop
backend/providers/{llm,image,storage}_provider.py   the three interfaces + their 10 implementations
backend/alembic/                         schema migrations (prod runs `alembic upgrade head`)
frontend/src/                            React + TS + Vite UI (library, book viewer, jobs, settings)
frontend/src/services/apiClient.ts       axios client; VITE_API_BASE_URL + VITE_API_KEY (build-time inlined)
render.yaml                              Render Blueprint: Docker API service + static frontend site
docs/render-deployment.md                deployment runbook (Render + Neon + Nhost, all free tiers)
docs/bug-audit-2026-09.md                read-only audit, 35 findings P0–P3
```

## Quick start (local)

```bash
conda create -n booktures python=3.11 && conda activate booktures
pip install -r requirements.txt            # full local dev (API + GPU inference stack)
# or: pip install -r backend/requirements-api.txt   # lean, cloud/API-only, no torch

cd backend
docker compose up -d                       # local Postgres (pgvector)
cp .env.example .env                       # set DATABASE_URL / provider keys
alembic upgrade head
uvicorn main:app --reload                  # http://127.0.0.1:8000  (docs at /docs)
```

```bash
cd frontend
npm install
npm run dev                                # http://localhost:5173
```

Provider selection is via env (or the Settings UI, which persists to a DB table):
`LLM_PROVIDER`, `IMAGE_PROVIDER`, `STORAGE_PROVIDER`, plus the matching keys.

## Deploying (Render free tier)

`render.yaml` is a Blueprint defining both services — a Docker web service for
the API and a static site for the frontend (`rootDir: frontend`). Secrets
(`DATABASE_URL`, `APP_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `CF_*`,
`NHOST_ADMIN_SECRET`, `VITE_API_KEY`) are `sync: false` and entered in the
dashboard; everything else is baked in. Backing services: **Neon** serverless
Postgres, **Nhost** Storage, **Groq** + **Gemini** (text), **Cloudflare Workers
AI** (images). Total cost **$0/month**, at the cost of a 15-minute idle sleep
and ~30–60 s cold start. Full runbook: [`docs/render-deployment.md`](docs/render-deployment.md).

## Method notes

**Multi-candidate extraction.** A PDF has no reliable text layer, so no single
library is trusted. `pdf_service` runs PyMuPDF and pdfplumber (three modes),
scores each result with `_extraction_score` — word count and alpha-token ratio
up; symbol ratio, single-letter tokens and broken hyphens down — and keeps the
best per page. This is a label-free proxy for character error rate (CER).

**OCR is a scored fallback, not a default.** OCR only runs when the digital text
looks weak (`_should_try_ocr`), at `OCR_DPI=200` (down from 300 to fit 512 MB
RAM), and its output only wins if it out-scores the digital text. An LLM repairs
common OCR errors, capped at `OCR_REPAIR_MAX_CALLS=40` process-wide so a long
scan can't exhaust the Groq rate limit.

**Character consistency is architected, not measured.** Image models are
stateless between calls, so "Alice" drifts across pages. The Visual Bible pins
*described* traits and is inlined into every image prompt. It does **not** pin a
face — reference-image conditioning (IP-Adapter) is the real fix and isn't
built.

**Union-find alias grouping.** `_group_aliases()` merges "Holmes" / "Sherlock" /
"Mr. Sherlock Holmes" into one entry: honorifics stripped, pairwise
`rapidfuzz.partial_ratio > 85`, merged with a disjoint-set structure so grouping
is **transitive** and **order-independent** — properties the naive
"first-match-wins" approach lacks.

**Single-pass trait extraction.** Instead of one LLM call per character, ~120k
characters of the book go to a long-context model (Gemini) in one call. Cheaper,
and it catches characters introduced late.

**Fail loud.** `CharacterExtractionError` fails the job if zero characters are
found (previously the book "completed" with blank art). Escape hatch:
`CHARACTER_EXTRACTION_ALLOW_EMPTY=true`.

**Deterministic rendering.** `PageAsset.seed = chunk.id`, so re-running a page
produces the identical image.

## Constraints & limitations (carried over honestly from `docs/bug-audit-2026-09.md`)

- **Single in-process worker.** One job at a time per process; can't scale to a
  second instance without double-processing (no heartbeat column). A deploy or a
  free-tier sleep restarts an in-flight analysis job **from page 0** — there is
  no checkpointing, and retry can duplicate `DocumentChunk` rows.
- **A paused job spins the worker in place** — every other queued job stalls
  until it resumes or is cancelled.
- **A provider returning `None` for one page is silently skipped** — the book
  completes with a missing page and no flag (partly fixed: empty Visual Bible
  and empty prompts now fail / get a fallback).
- **Auth is a single shared `X-API-Key`**, baked into the frontend bundle at
  build time. It keeps casual traffic out; it is not a real auth boundary.
- **No evaluation harness.** CLIPScore, face-embedding distance, CER/WER are
  understood (see `docs/` interview notes) but not wired up — changes are judged
  by eyeballing output.
- **Free-tier economics shape the design**: 512 MB RAM forced OCR to 200 DPI and
  character extraction onto a single LLM call instead of a local NER model;
  Groq's rate limit forced the OCR-repair budget.
- **Per-page analysis is serial** — ~3 LLM calls × N pages on one thread,
  10–30 min for a 200-page book. Parallelising under a rate-limit semaphore is
  the highest-value performance change.

## Author

Developed by Pratik. Intended for educational and research purposes.
