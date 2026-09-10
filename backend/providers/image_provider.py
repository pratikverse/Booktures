"""
Image provider abstraction. Swap between local Diffusers inference and a
cloud provider (pollinations, workers-ai, gemini) via the IMAGE_PROVIDER env
var, without touching call sites.
"""

import os
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STORAGE_PATH = Path(__file__).resolve().parents[1] / "storage" / "illustrations"

MODE_ALIASES = {"quality", "balanced", "fast", "custom"}
MODE_DEFAULTS = {
    "quality": {"model": "SG161222/RealVisXL_V4.0", "steps": 24, "guidance": 5.5, "width": 768, "height": 768},
    "balanced": {"model": "segmind/SSD-1B", "steps": 12, "guidance": 8.0, "width": 768, "height": 768},
    "fast": {"model": "stabilityai/sd-turbo", "steps": 4, "guidance": 1.5, "width": 512, "height": 768},
}


def _resolve_generation_config():
    image_mode = os.getenv("IMAGE_PRESET", "balanced").strip().lower()
    if image_mode not in MODE_ALIASES:
        image_mode = "balanced"

    defaults = MODE_DEFAULTS["balanced"] if image_mode == "custom" else MODE_DEFAULTS[image_mode]
    return {
        "model": os.getenv("DIFFUSION_MODEL", defaults["model"]),
        "steps": int(os.getenv("IMAGE_STEPS", str(defaults["steps"]))),
        "guidance": float(os.getenv("IMAGE_GUIDANCE", str(defaults["guidance"]))),
        "width": int(os.getenv("IMAGE_WIDTH", str(defaults["width"]))),
        "height": int(os.getenv("IMAGE_HEIGHT", str(defaults["height"]))),
    }


def _save(image, book_id: int, page_num: int) -> Optional[str]:
    from io import BytesIO
    from providers.storage_provider import get_storage_provider

    buf = BytesIO()
    image.save(buf, format="PNG")
    key = f"illustrations/book_{book_id}/page_{page_num}.png"
    return get_storage_provider().save(buf.getvalue(), key, content_type="image/png")


class ImageProvider:
    def render(self, prompt: str, negative_prompt: str, book_id: int, page_num: int, seed: int) -> Optional[str]:
        raise NotImplementedError


class DiffusersProvider(ImageProvider):
    """Local inference via a diffusers pipeline (GPU/CPU)."""

    def __init__(self):
        self.pipe = None
        self.model_id = None
        self.device = None

    def _load_pipeline(self, model_id: str):
        import torch
        from diffusers import DiffusionPipeline

        if self.device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        if self.pipe is not None and self.model_id != model_id:
            logger.info(f"Model changed from '{self.model_id}' to '{model_id}', reloading pipeline...")
            self.pipe = None

        if self.pipe is not None:
            return

        self.model_id = model_id
        logger.info(f"Loading diffusion pipeline '{model_id}' on {self.device}...")
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.pipe = DiffusionPipeline.from_pretrained(
            model_id,
            torch_dtype=dtype,
            use_safetensors=True,
            variant="fp16" if self.device == "cuda" else None,
        )
        self.pipe.to(self.device)
        if self.device == "cuda":
            self.pipe.enable_attention_slicing()
        logger.info("Diffusion pipeline loaded successfully.")

    def render(self, prompt: str, negative_prompt: str, book_id: int, page_num: int, seed: int) -> Optional[str]:
        import torch

        config = _resolve_generation_config()
        self._load_pipeline(config["model"])
        generator = torch.Generator(device=self.device).manual_seed(seed)

        try:
            image = self.pipe(
                prompt=prompt,
                negative_prompt=negative_prompt,
                num_inference_steps=config["steps"],
                guidance_scale=config["guidance"],
                width=config["width"],
                height=config["height"],
                generator=generator,
            ).images[0]

            if not image.getbbox():
                logger.warning(f"Safety filter triggered or generation failed (black frame) for book {book_id} p{page_num}")
                return None

            return _save(image, book_id, page_num)
        except Exception as e:
            logger.error(f"Image generation failed: {e}")
            return None


class PollinationsProvider(ImageProvider):
    """Free, keyless cloud image generation via image.pollinations.ai."""

    def render(self, prompt: str, negative_prompt: str, book_id: int, page_num: int, seed: int) -> Optional[str]:
        import httpx
        from io import BytesIO
        from PIL import Image
        from urllib.parse import quote

        config = _resolve_generation_config()
        url = (
            f"https://image.pollinations.ai/prompt/{quote(prompt)}"
            f"?width={config['width']}&height={config['height']}&seed={seed}&nologo=true"
        )
        try:
            response = httpx.get(url, timeout=float(os.getenv("POLLINATIONS_TIMEOUT_SECONDS", "60.0")))
            response.raise_for_status()
            image = Image.open(BytesIO(response.content)).convert("RGB")
            return _save(image, book_id, page_num)
        except Exception as e:
            logger.error(f"Pollinations image generation failed: {e}")
            return None


class CloudflareWorkersAIProvider(ImageProvider):
    """Cloud image generation via Cloudflare Workers AI."""

    def render(self, prompt: str, negative_prompt: str, book_id: int, page_num: int, seed: int) -> Optional[str]:
        import httpx
        import base64
        from io import BytesIO
        from PIL import Image

        account_id = os.getenv("CF_ACCOUNT_ID")
        api_token = os.getenv("CF_API_TOKEN")
        model = os.getenv("CF_WORKERS_AI_MODEL", "@cf/stabilityai/stable-diffusion-xl-base-1.0")
        if not account_id or not api_token:
            logger.warning("CF_ACCOUNT_ID/CF_API_TOKEN not set; skipping image generation.")
            return None

        config = _resolve_generation_config()
        try:
            response = httpx.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}",
                headers={"Authorization": f"Bearer {api_token}"},
                json={
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "width": config["width"],
                    "height": config["height"],
                    "seed": seed,
                    "num_steps": min(config["steps"], 20),
                },
                timeout=float(os.getenv("CF_TIMEOUT_SECONDS", "60.0")),
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if content_type.startswith("image/"):
                image_bytes = response.content
            else:
                data = response.json()
                b64 = (data.get("result") or {}).get("image")
                if not b64:
                    logger.error(f"Unexpected Workers AI response: {data}")
                    return None
                image_bytes = base64.b64decode(b64)

            image = Image.open(BytesIO(image_bytes)).convert("RGB")
            return _save(image, book_id, page_num)
        except Exception as e:
            logger.error(f"Cloudflare Workers AI image generation failed: {e}")
            return None


class GeminiImageProvider(ImageProvider):
    """Cloud image generation via the Gemini API (gemini-2.5-flash-image).

    NOT free: the image model has a free-tier quota of 0 (unlike the Gemini
    text models). The GEMINI_API_KEY's Google Cloud project needs billing
    enabled for this to work; then it's cheap, not free.

    Reuses GEMINI_API_KEY. No seed or size controls - output is resized to the
    configured dimensions. Negative prompts aren't a first-class parameter, so
    the negative is folded into the text prompt.

    Character consistency: Gemini can accept reference images, which would let
    it match a character to earlier pages. Wiring that needs _run_image_pipeline
    to pass the previous page's illustration bytes through render() - a follow-up.
    """

    def render(self, prompt: str, negative_prompt: str, book_id: int, page_num: int, seed: int) -> Optional[str]:
        import httpx
        import base64
        from io import BytesIO
        from PIL import Image

        api_key = os.getenv("GEMINI_API_KEY")
        model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; skipping image generation.")
            return None

        config = _resolve_generation_config()
        text = f"{prompt}\n\nAvoid: {negative_prompt}" if negative_prompt else prompt

        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
                },
                timeout=float(os.getenv("GEMINI_IMAGE_TIMEOUT_SECONDS", "90.0")),
            )
            response.raise_for_status()
            data = response.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            b64 = next((p["inlineData"]["data"] for p in parts if p.get("inlineData")), None)
            if not b64:
                logger.error(f"No image in Gemini response: {data}")
                return None

            image = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
            image = image.resize((config["width"], config["height"]))
            return _save(image, book_id, page_num)
        except Exception as e:
            logger.error(f"Gemini image generation failed: {e}")
            return None


_providers = {
    "diffusers": DiffusersProvider,
    "pollinations": PollinationsProvider,
    "workers-ai": CloudflareWorkersAIProvider,
    "gemini": GeminiImageProvider,
}

_instance_cache = {}


def list_image_providers() -> list[str]:
    """Registered IMAGE_PROVIDER names, for the Settings dropdown / validation."""
    return list(_providers)


def get_image_provider() -> ImageProvider:
    name = os.getenv("IMAGE_PROVIDER", "diffusers").strip().lower()
    cls = _providers.get(name, DiffusersProvider)
    if name not in _instance_cache:
        _instance_cache[name] = cls()
    return _instance_cache[name]
