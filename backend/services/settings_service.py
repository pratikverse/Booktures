import os
import subprocess
import httpx
from database import SessionLocal
from models import AppSetting

def update_setting(key: str, value: str):
    """Persists a setting to the DB-backed settings table and the active process environment.

    DB-backed (not .env) so this works on read-only/ephemeral filesystems in production.
    """
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    finally:
        db.close()
    os.environ[key] = value

def load_persisted_settings():
    """Loads all DB-persisted settings into the process environment. Call once at startup."""
    db = SessionLocal()
    try:
        for row in db.query(AppSetting).all():
            os.environ[row.key] = row.value
    finally:
        db.close()

def get_available_ollama_models(base_url: str | None = None):
    """Detects available models from Ollama's HTTP API, with CLI fallback."""
    target_base = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
    try:
        r = httpx.get(f"{target_base}/api/tags", timeout=8.0)
        r.raise_for_status()
        data = r.json()
        names = []
        for item in data.get("models", []):
            name = item.get("name")
            if name and name not in names:
                names.append(name)
        if names:
            return names
    except Exception:
        pass

    # Fallback for environments where HTTP access is blocked but CLI works.
    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.splitlines():
            row = line.strip()
            if not row or row.upper().startswith("NAME "):
                continue
            name = row.split()[0]
            if name and name not in models:
                models.append(name)
        return models
    except Exception:
        return []
