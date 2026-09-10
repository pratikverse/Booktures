# Bug & Loose-Ends Audit — 2026-09-10

Read-only review of the committed backend + frontend API layer. No code changed during the
audit. Check items off as they are addressed. Line references were accurate at audit time and
may drift.

Severity: **P0** breaks the core flow silently · **P1** correctness/robustness ·
**P2** security/ops · **P3** schema/dead code/drift.

---

## P0 — silently corrupts output or breaks core flows

- [ ] **1. Resume/retry of a `book_pipeline` job re-runs from page 0.**
  `api/routes.py` `retry`/`resume` set `status="queued"`; worker re-runs
  `_run_analysis_pipeline` from scratch → duplicate `DocumentChunk` rows, then
  `PageAsset.chunk_id` (`unique=True`) IntegrityError → job `failed`. No checkpoint / skip
  logic. Image pipeline is partly guarded (`if not asset.image_path`) but still re-generates
  prompts.
  _Fix idea:_ before re-running, skip pages that already have a chunk/asset, or wipe partial
  rows on retry.

- [ ] **2. A paused job blocks the whole queue.**
  `generation_queue_service.py` `while job.status == "paused": time.sleep(2)` on the single
  worker thread. Every other queued job stalls until resume/cancel.
  _Fix idea:_ on pause, return the job to a `paused` state and `continue` the worker loop;
  re-claim when resumed.

- [ ] **3. LLM/image failure = "completed" book with blank/garbage art, no error surfaced.**
  Chain: `providers/llm_provider.py` (all providers → `""` on any exception, no retry) →
  `services/prompt_service.py:56-58` (`""` → returns just `f"{style_desc}, "`) →
  near-empty `PageAsset.image_prompt` → `image_provider` renders garbage or returns `None` →
  `generation_queue_service.py:203-210` silently skips falsy `img_path` → job/book still
  `completed`.
  _Fix idea:_ providers raise on hard failure; worker distinguishes "no result" and marks the
  page (or job) degraded/failed.

- [ ] **4. `GeminiProvider` KeyErrors on safety-blocked responses, swallowed to `""`.**
  `providers/llm_provider.py:121+` indexes
  `data["candidates"][0]["content"]["parts"][0]["text"]`; blocked/empty candidates →
  `KeyError`/`IndexError` → generic `except` → `""` → feeds #3.

- [x] **5. Missing spaCy model → book silently gets zero characters.** *(fixed 2026-09-10)*
  `services/character_service.py` — was: `spacy.load` `OSError` → `nlp = None`; `nlp is None`
  logs + bare `return`; LLM path returning `[]` did the same → empty Visual Bible, pipeline
  continues.
  Now: `process_book_characters` raises `CharacterExtractionError` when no `Character` rows
  were persisted (either mode), and when spaCy mode is selected without the model installed.
  The job worker's `_execute_job` catch turns it into `job.status="failed"` with the message
  in `status_note`. Escape hatch: `CHARACTER_EXTRACTION_ALLOW_EMPTY=true`. Hosted config also
  runs `CHARACTER_EXTRACTION_MODE=llm` (Gemini whole-book pass) so the model isn't needed.

- [ ] **6. `previous_summaries_text` join crashes a running job on a `NULL` summary.**
  `generation_queue_service.py:133`
  `" ".join([pa.visual_summary for pa in prev_page_assets])` → `TypeError` when
  `PageAsset.visual_summary` is `NULL`.
  _Fix idea:_ `" ".join(pa.visual_summary or "" for pa in ...)`.

---

## P1 — correctness / robustness

- [x] **7b. `OLLAMA_ENABLED=False` silently disabled ALL LLM enrichment, cloud included.** *(fixed 2026-09-10)*
  `pdf_service.ollama_generate` + 3 other sites returned early on `not OLLAMA_ENABLED`
  regardless of `LLM_PROVIDER`. With the hosted config (`OLLAMA_ENABLED=False`,
  `LLM_PROVIDER=groq`) every page summary, image-prompt rewrite, OCR repair, and
  header/weak-text classification was a no-op — the app ran entirely on heuristic
  fallbacks. Now gated by `_llm_enabled()`: `OLLAMA_ENABLED` only affects Ollama;
  a cloud provider runs regardless.

- [x] **7c. Default `GROQ_MODEL` (`llama-3.1-8b-instant`) 404s — Groq retired the Llama 3.x models.** *(fixed 2026-09-10)*
  Every Groq call failed with `model_not_found` → `""` → fallbacks. Default is now
  `openai/gpt-oss-20b` (`llm_provider.py`, `.env.example`, `fly.toml`). **Action: update
  `GROQ_MODEL` in your local `.env`** (currently pinned to the dead model).

- [ ] **7. Runtime settings changes don't take effect (import-time env binding).**
  `PUT /settings` writes DB + `os.environ`, but `pdf_service` (`OLLAMA_BASE_URL`,
  `OLLAMA_DEFAULT_MODEL`, `OLLAMA_FAST_MODEL`, `OLLAMA_ENABLED`, OCR thresholds),
  `character_service.OLLAMA_MODEL`, `prompt_service.py:6 OLLAMA_MODEL` are module-level
  constants frozen before `load_persisted_settings()`. `GET /settings`
  (`routes.py:298,306`) reads the frozen `pdf_service.OLLAMA_DEFAULT_MODEL`, so the UI shows
  the stale value. `IMAGE_*`, `LLM_PROVIDER`, `IMAGE_PROVIDER` *are* read live → inconsistent.
  _Fix idea:_ read settings through a helper at call time, not module import.
  *Partially fixed 2026-09-10:* the Ollama subset is now live — `OllamaProvider.generate`
  reads `OLLAMA_BASE_URL`/`OLLAMA_DEFAULT_MODEL`/`OLLAMA_TIMEOUT_SECONDS` per call,
  `ollama_generate` resolves the model live, `GET /settings` reads from env not the frozen
  constants, and `PUT /settings` accepts `llm_provider`. Still frozen: `OLLAMA_FAST_MODEL`
  (fast-model optimisation is bypassed when a model is set in Settings), OCR thresholds,
  `character_service`/`prompt_service` `OLLAMA_MODEL` (harmless - overridden in
  `ollama_generate`).

- [ ] **8. Non-numeric setting values crash rendering / startup.**
  `providers/image_provider.py` `_resolve_generation_config` — `int(os.getenv("IMAGE_STEPS"))`,
  `float(...)` guidance/width/height → `ValueError` → render/job fail.
  `routes.py:20` `MAX_UPLOAD_BYTES` cast at import → startup crash.
  `routes.py:310-313` `get_settings` casts → `GET /settings` 500.
  _Fix idea:_ validate/clamp on write in `update_settings`; safe-cast helpers with defaults.

- [ ] **9. `_reset_orphaned_jobs` blindly requeues all running jobs.**
  `generation_queue_service.py:47-57`; a worker starts per process (`main.py:47`). With
  `uvicorn --workers N`, each restart requeues jobs another worker is actively running →
  duplicate processing (compounds #1). No `updated_at`/heartbeat on `Job` to tell dead from
  live.

- [ ] **10. Cancelled job leaves the book stuck in `processing`.**
  `generation_queue_service.py:115-117,180-182` — worker just `return`s; `book.status` stays
  `processing`/`generating_images` forever.

- [ ] **11. "No extractable text" → job `completed` but book `failed`.**
  `generation_queue_service.py:96-102`. Jobs page and Library disagree.

- [ ] **12. `filename` used unsanitised in the storage key (path traversal).**
  `pdf_service.save_pdf` — `key = f"pdfs/{uuid4()}_{filename}"`; `routes.py:85` only checks
  extension. `../` or `/` in the name can escape `storage/` for `LocalStorageProvider`.
  _Fix idea:_ `os.path.basename` + strip separators before building the key.

- [ ] **13. `upload_book` blocks the event loop.**
  `routes.py:76` `async def` calling synchronous network-bound `pdf_service.save_pdf`.
  _Fix idea:_ make the handler `def`, or `run_in_threadpool` the save.

- [ ] **14. R2 upload succeeds but returns `None` when `R2_PUBLIC_URL` unset → orphaned file.**
  `providers/storage_provider.py` `CloudflareR2Provider.save`.

- [ ] **15. `delete_book` drops DB rows even when file deletion failed.**
  `routes.py:151-165` — `storage.delete()` return ignored; Supabase/R2 `delete` silently
  no-ops when unconfigured. Orphaned storage, no trace.

- [x] **16. Engine has no `pool_pre_ping` / `pool_recycle`.**
  `database.py:30`. Managed Postgres drops idle connections → "server closed the connection
  unexpectedly" on the idle worker loop and pooled sessions.
  _Fixed (Neon migration):_ `create_engine(DATABASE_URL, pool_pre_ping=True,
  pool_recycle=300, pool_size=5, max_overflow=5)`.

- [ ] **17. `file.filename` can be `None`.**
  `routes.py:85` `file.filename.lower()` → `AttributeError` → 500 instead of 400.

- [ ] **18. `_normalize_text` CamelCase splitter corrupts real words.**
  `pdf_service._normalize_text` — `re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)` →
  "McDonald"→"Mc Donald", "iPhone"→"i Phone" in text that feeds every downstream prompt.

- [ ] **19. Alias substring matching inflates mention counts.**
  `character_service._process_characters_llm._find_chunk_ids` — lowercased substring match;
  short aliases ("Al","Ed","Jo") match inside "also","editor","join".

- [ ] **20. Remote-PDF fetch failure hard-fails the whole job.**
  `pdf_service.extract_text_by_page` — `httpx.get(...).raise_for_status()` propagates, no
  retry; also loads the full PDF into memory twice.

---

## P2 — security / operational

- [ ] **21. Auth silently disabled when `APP_API_KEY` unset — no startup warning.**
  `api/auth.py:12-13`. Also `x_api_key != expected` is not constant-time.

- [ ] **22. API key shipped to every browser.**
  `frontend/src/services/apiClient.ts:11` — `VITE_API_KEY` baked into the bundle. Not a real
  auth boundary; document it and/or move to a real session model.

- [ ] **23. Error details leaked to clients.**
  `routes.py:103` `f"Failed to save PDF: {str(e)}"`; `main.py:99` `/ready` returns `str(e)`.
  No global exception handler.

- [ ] **24. Rate limiting is in-memory and per-process.**
  `api/limiter.py` — resets on restart, not shared across workers, `get_remote_address`
  sees the proxy IP without `X-Forwarded-For` config. `GET` lists and `/jobs/{id}/action`
  have no limit.

- [ ] **25. `/health` doesn't reflect worker health.**
  `main.py:87` always `{"status":"ok"}`; a dead worker thread stays green while jobs pile up.

---

## P3 — schema, dead code, drift

- [ ] **26. Two schema-management paths that don't agree.**
  `init_db()` runs `Base.metadata.create_all()` (`database.py:72`) *and* Alembic ships a
  single `0001_baseline`; nothing runs `alembic upgrade`. `create_all` never `ALTER`s, so
  post-baseline model changes have no path to an existing DB. Pick one.

- [ ] **27. Overlapping columns for the same data.**
  `DocumentChunk.illustration_path/summary/characters/scenes` duplicate
  `PageAsset.image_path/visual_summary`; worker writes both
  (`generation_queue_service.py:139-148`).

- [ ] **28. `page_characters` has no PK / unique constraint.**
  `models/character.py:6-11` — re-processing inserts duplicate `(character_id, chunk_id)`.

- [ ] **29. `Job` model nits.**
  `models/job.py:13` comment says `image_regeneration`, code uses `image_generation`. No
  `updated_at` (see #9); `job_type`/`status` unindexed (worker `WHERE status='queued'` scans).

- [ ] **30. Frontend/backend preset config duplicated.**
  `frontend/src/services/types.ts:83-108` `MODE_PRESETS` vs backend
  `image_provider.MODE_DEFAULTS` — already partly drifted.

- [ ] **31. Two disjoint style taxonomies.**
  `IMAGE_STYLE` (`normal/storybook/comic/cinematic`) vs
  `IMAGE_PRESET`/`image_mode` (`quality/balanced/fast/custom`) — nothing ties them.

- [ ] **32. Progress scaling inconsistent.**
  `frontend/src/services/books.ts` scales the list (`×100`) but not `getBook`; `routes.py:131`
  `get_book_details` omits `progress`. BookViewer vs Library disagree.

- [ ] **33. Unknown provider names silently fall back to the heavy local provider.**
  `get_image_provider()` → `DiffusersProvider` (imports torch); `get_storage_provider()` →
  `LocalStorageProvider`; `get_llm_provider` similar. A typo on an API-only box → torch
  import / OOM instead of a clear error.

- [ ] **34. Dead code / dead imports.**
  - `providers/image_provider.py:13` `STORAGE_PATH` unused.
  - `services/prompt_service.py:3` `from models import ...` unused.
  - `services/settings_service.get_mode_presets` — no callers.
  - `providers/llm_provider.py` top-level `from openai import OpenAI, APIError` — hard import
    for all providers (works today, `openai` is pinned, but needless coupling; lazy-import in
    `GroqProvider`).
  - `get_llm_provider()` returns a fresh instance per call → `GroqProvider._client` cache
    never survives; new `OpenAI` client per LLM call.

- [ ] **35. `PollinationsProvider` ignores `negative_prompt`.**
  `providers/image_provider.py` — drops the `VISUAL_STYLES` negative; long prompts also risk
  GET URL-length limits.

---

## Suggested order

1. #1, #2, #3 — core "upload → illustrated book" flow reliability + visible failures.
2. #7, #8 — settings UI is partly non-functional / a footgun.
3. #9, #10, #11, #6 — job lifecycle correctness.
4. #12, #21, #23 — security basics before any public deploy.
5. #26 — decide the schema story before the next model change.
