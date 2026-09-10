"""
LLM provider abstraction. Swap between local Ollama and a cloud free-tier
provider (Groq) via the LLM_PROVIDER env var, without touching call sites.

Note on the `openai` dependency (see requirements-api.txt): this project has
never called OpenAI's own API. `openai==...` is listed because GroqProvider
below uses the `openai` Python package as a plain HTTP client, pointed at
Groq's OpenAI-*compatible* endpoint (GROQ_BASE_URL, i.e.
`https://api.groq.com/openai/v1`) via the `base_url` argument — the same
pattern works for Together, vLLM, and any other provider that implements the
same `/v1/chat/completions` wire format. Before this fix, GroqProvider called
that endpoint with raw `httpx.post()` instead, which made the `openai`
dependency dead code and made "why does this project depend on openai?" an
unnecessarily confusing question to answer. It no longer is.
"""

import os
import logging
import httpx
from openai import OpenAI, APIError

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180.0"))
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Groq retired the Llama 3.1/3.3 hosted models; gpt-oss-20b is the current
# fast general-purpose free-tier option. Override with GROQ_MODEL.
GROQ_DEFAULT_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_TIMEOUT_SECONDS = float(os.getenv("GROQ_TIMEOUT_SECONDS", "60.0"))

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60.0"))


class LLMProvider:
    def generate(self, prompt: str, system: str = "", model: str | None = None) -> str:
        raise NotImplementedError


class OllamaProvider(LLMProvider):
    """Local inference via Ollama's HTTP API.

    Reads OLLAMA_BASE_URL / OLLAMA_DEFAULT_MODEL / OLLAMA_TIMEOUT_SECONDS from the
    environment on every call, not at import, so the Settings page can point this
    at a different Ollama (e.g. a tunnelled local instance) without a restart.
    """

    def generate(self, prompt: str, system: str = "", model: str | None = None) -> str:
        base_url = os.getenv("OLLAMA_BASE_URL", OLLAMA_BASE_URL).rstrip("/")
        timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", str(OLLAMA_TIMEOUT_SECONDS)))
        payload = {
            "model": model or os.getenv("OLLAMA_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL),
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system

        try:
            response = httpx.post(
                f"{base_url}/api/generate",
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except httpx.HTTPError as exc:
            logger.warning("Ollama HTTP request failed (model=%s): %s", payload["model"], exc)
        except Exception as exc:
            logger.warning("Unexpected Ollama error (model=%s): %s", payload["model"], exc)
        return ""


class GroqProvider(LLMProvider):
    """Cloud inference via Groq's free-tier OpenAI-compatible API.

    Uses the `openai` package purely as an HTTP client: same
    /v1/chat/completions wire format, different `base_url` and API key.
    Nothing here ever talks to OpenAI's own servers.
    """

    def __init__(self) -> None:
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI | None:
        if self._client is not None:
            return self._client
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.warning("GROQ_API_KEY not set; skipping LLM call.")
            return None
        self._client = OpenAI(
            api_key=api_key,
            base_url=GROQ_BASE_URL,
            timeout=GROQ_TIMEOUT_SECONDS,
        )
        return self._client

    def generate(self, prompt: str, system: str = "", model: str | None = None) -> str:
        client = self._get_client()
        if client is None:
            return ""

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = client.chat.completions.create(
                model=model or GROQ_DEFAULT_MODEL,
                messages=messages,
            )
            return (response.choices[0].message.content or "").strip()
        except APIError as exc:
            logger.warning("Groq request failed: %s", exc)
        except Exception as exc:
            logger.warning("Unexpected Groq error: %s", exc)
        return ""


class GeminiProvider(LLMProvider):
    """Cloud inference via Google's Gemini free-tier API."""

    def generate(self, prompt: str, system: str = "", model: str | None = None) -> str:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; skipping LLM call.")
            return ""

        body = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        try:
            response = httpx.post(
                f"{GEMINI_BASE_URL}/models/{model or GEMINI_DEFAULT_MODEL}:generateContent",
                params={"key": api_key},
                json=body,
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except httpx.HTTPError as exc:
            logger.warning("Gemini HTTP request failed: %s", exc)
        except Exception as exc:
            logger.warning("Unexpected Gemini error: %s", exc)
        return ""


_providers = {
    "ollama": OllamaProvider,
    "groq": GroqProvider,
    "gemini": GeminiProvider,
}


def get_llm_provider(name: str | None = None) -> LLMProvider:
    """Return an LLM provider instance.

    Pass `name` to force a specific provider (e.g. character extraction wanting
    Gemini's large context regardless of the global LLM_PROVIDER); omit it to use
    the LLM_PROVIDER env var. Unknown names fall back to Ollama.
    """
    resolved = (name or os.getenv("LLM_PROVIDER", "ollama")).strip().lower()
    cls = _providers.get(resolved, OllamaProvider)
    return cls()
