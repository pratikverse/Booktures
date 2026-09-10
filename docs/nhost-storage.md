# Nhost Storage

Storage backend option added alongside `local` / `supabase` / `r2`.
Select it with `STORAGE_PROVIDER=nhost`.

## Nhost project

- Project: **Booktures** (org "Pratik's Organization", Starter plan)
- Subdomain: `jhowkmqddbkqwgwbjrek`
- Region: `ap-south-1` (Mumbai)
- Bucket: `default`

## One-time console setup (done 2026-09-10)

Storage → Permissions → `public` role → **Download** → "Without any checks" → Save.

This makes `GET /v1/files/<id>` work without auth so illustration URLs render in
the browser. Upload / Replace / Delete stay admin-only — the backend performs
those with the admin secret. Mirrors the current Supabase public-read model.

## Environment variables

Set these locally in `backend/.env` and on Render (Environment tab):

```
STORAGE_PROVIDER=nhost
NHOST_STORAGE_URL=https://jhowkmqddbkqwgwbjrek.storage.ap-south-1.nhost.run/v1
NHOST_ADMIN_SECRET=<copy from Nhost console → project → Hasura → "Admin Secret" copy button>
NHOST_BUCKET=default
```

The admin secret is a credential — keep it out of git (it only ever lives in
`.env` / the Render dashboard, both untracked).

## How the provider works

`backend/providers/storage_provider.py` → `NhostStorageProvider`:

- Nhost addresses files by a server-side UUID, not a path. The provider derives
  a stable UUID from the storage key (`uuid5`), deletes any existing file with
  that id, then uploads with it — so retries replace rather than duplicate.
- Stores the full download URL (`.../v1/files/<uuid>`) in `book.file_path` /
  `chunk.illustration_path`. `_public_storage_url` and the frontend `fileUrl`
  pass full URLs through unchanged.
- `routes.py._storage_key_from_path` recognises `/v1/files/` URLs so
  `delete_book` can recover the file id and delete the remote object.

## Migration note

Existing books/illustrations keep their old Supabase URLs in the DB and keep
working. Only new uploads go to Nhost. For a clean cut, re-upload or write a
one-off migration.

## Known gaps (see docs/bug-audit-2026-09.md)

- A bad/missing `NHOST_*` value → `save()` returns `None` silently → job
  "completes" with missing files (#3 / #14).
- Unknown `STORAGE_PROVIDER` still silently falls back to local (#33).
- Not yet tested against the live project end-to-end — verify one upload.
