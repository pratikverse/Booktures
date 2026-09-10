"""
Storage provider abstraction for generated output files (illustrations).
Local disk by default; Nhost Storage or Cloudflare R2 for deployments on
ephemeral/read-only filesystems. Selected via STORAGE_PROVIDER.
"""

import os
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path(__file__).resolve().parents[1] / "storage"


class StorageProvider:
    def save(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> Optional[str]:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalStorageProvider(StorageProvider):
    """Writes to the local storage/ dir, served via the /storage static mount."""

    def save(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> Optional[str]:
        path = STORAGE_ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"storage/{key}"

    def delete(self, key: str) -> None:
        path = STORAGE_ROOT / key
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Local delete failed for {key}: {e}")


class CloudflareR2Provider(StorageProvider):
    """Uploads to a Cloudflare R2 bucket via its S3-compatible API."""

    def save(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> Optional[str]:
        import boto3

        account_id = os.getenv("R2_ACCOUNT_ID")
        bucket = os.getenv("R2_BUCKET")
        public_base = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
        if not account_id or not bucket:
            logger.warning("R2_ACCOUNT_ID/R2_BUCKET not set; skipping upload.")
            return None

        try:
            client = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
                region_name="auto",
            )
            client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
            return f"{public_base}/{key}" if public_base else None
        except Exception as e:
            logger.error(f"R2 upload failed for {key}: {e}")
            return None

    def delete(self, key: str) -> None:
        import boto3

        account_id = os.getenv("R2_ACCOUNT_ID")
        bucket = os.getenv("R2_BUCKET")
        if not account_id or not bucket:
            return
        try:
            client = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
                region_name="auto",
            )
            client.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            logger.warning(f"R2 delete failed for {key}: {e}")


class NhostStorageProvider(StorageProvider):
    """Uploads to Nhost Storage (Hasura Storage) via its REST API.

    Nhost addresses every file by a server-side UUID rather than by an
    arbitrary path key, so we derive a *stable* UUID from the key
    (uuid5) and hand it to Nhost as the file id. That keeps re-uploads of
    the same key idempotent (delete-then-create) instead of piling up
    duplicate files, and lets us build the download URL without parsing
    the response.

    Requires env:
      NHOST_STORAGE_URL   e.g. https://<subdomain>.storage.<region>.nhost.run/v1
      NHOST_ADMIN_SECRET  the project's admin secret (server-side only)
      NHOST_BUCKET        optional, defaults to "default"

    The target bucket must allow public downloads for the illustrations to
    render in the browser: in the Nhost dashboard give the `public` role a
    `select` permission on `storage.files` (or use a bucket configured for
    public access). Otherwise GET /v1/files/<id> returns 403 and you'd need
    presigned URLs, which expire and can't be stored in the DB.
    """

    def _config(self):
        base = os.getenv("NHOST_STORAGE_URL", "").rstrip("/")
        admin_secret = os.getenv("NHOST_ADMIN_SECRET")
        bucket = os.getenv("NHOST_BUCKET", "default")
        return base, admin_secret, bucket

    def _file_id(self, key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    def save(self, data: bytes, key: str, content_type: str = "application/octet-stream") -> Optional[str]:
        import httpx

        base, admin_secret, bucket = self._config()
        if not base or not admin_secret:
            logger.warning("NHOST_STORAGE_URL/NHOST_ADMIN_SECRET not set; skipping upload.")
            return None

        file_id = self._file_id(key)
        filename = key.rsplit("/", 1)[-1]
        headers = {"x-hasura-admin-secret": admin_secret}

        try:
            # Nhost has no upsert; drop any existing file with this id first.
            httpx.delete(f"{base}/files/{file_id}", headers=headers, timeout=30.0)

            response = httpx.post(
                f"{base}/files",
                headers=headers,
                data={
                    "bucket-id": bucket,
                    "metadata[]": json.dumps({"id": file_id, "name": key}),
                },
                files={"file[]": (filename, data, content_type)},
                timeout=60.0,
            )
            response.raise_for_status()
            return f"{base}/files/{file_id}"
        except Exception as e:
            logger.error(f"Nhost upload failed for {key}: {e}")
            return None

    def delete(self, key: str) -> None:
        import httpx

        base, admin_secret, _ = self._config()
        if not base or not admin_secret:
            return
        # `key` may arrive as a bare uuid or as the full download URL
        # (.../v1/files/<uuid>) recorded in the DB - take the last segment.
        file_id = key.rstrip("/").rsplit("/", 1)[-1]
        try:
            httpx.delete(
                f"{base}/files/{file_id}",
                headers={"x-hasura-admin-secret": admin_secret},
                timeout=30.0,
            )
        except Exception as e:
            logger.warning(f"Nhost delete failed for {key}: {e}")


_providers = {
    "local": LocalStorageProvider,
    "nhost": NhostStorageProvider,
    "r2": CloudflareR2Provider,
}

_instance_cache = {}


def get_storage_provider() -> StorageProvider:
    name = os.getenv("STORAGE_PROVIDER", "local").strip().lower()
    cls = _providers.get(name, LocalStorageProvider)
    if name not in _instance_cache:
        _instance_cache[name] = cls()
    return _instance_cache[name]
