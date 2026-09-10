import os
import re
from services.pdf_service import ollama_generate

OLLAMA_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")

VISUAL_STYLES = {
    "normal": {
        "style": "clean illustration, natural colors, clear subjects, soft even lighting",
        "negative": (
            "low quality, blurry, distorted, deformed, extra limbs, extra fingers, "
            "text, letters, caption, watermark, signature, logo, frame, border"
        ),
    },
    "storybook": {
        "style": (
            "illustrated children's storybook art, warm painterly detail, cinematic "
            "composition, soft directional light, expressive readable faces, cohesive palette"
        ),
        "negative": (
            "photo, 3d render, blurry, low detail, distorted face, deformed hands, "
            "extra limbs, extra fingers, mutated anatomy, text, letters, words, caption, "
            "speech bubble, watermark, signature, logo, frame, border, cropped, "
            "out of frame, harsh lighting, oversaturated"
        ),
    },
    "comic": {
        "style": (
            "graphic novel panel, bold clean linework, flat cel shading, dramatic "
            "contrast, dynamic angle, expressive acting"
        ),
        "negative": (
            "photo-realistic skin, soft blur, muddy colors, distorted face, extra "
            "fingers, extra limbs, text, gibberish letters, caption, speech bubble, "
            "watermark, signature, logo, panel border, gutter, cropped, out of frame"
        ),
    },
    "cinematic": {
        "style": (
            "cinematic digital painting, atmospheric depth, volumetric light, "
            "expressive rim lighting, grounded character continuity, filmic color"
        ),
        "negative": (
            "flat lighting, low contrast, washed out, blurry, deformed anatomy, "
            "extra limbs, extra fingers, text, letters, caption, watermark, signature, "
            "logo, frame, border, cropped, out of frame"
        ),
    },
}

# Scene strings that carry no information (fallbacks from character_service) - drop
# them rather than feeding "Standard setting" into the image prompt.
_SCENE_NOISE = {"", "unknown", "n/a", "none", "standard setting", "standard", "no scene"}

_MARKUP_PREFIX_RE = re.compile(
    r"^\s*(here'?s?\b[^:]*:|image prompt:|prompt:|summary:|description:|output:|scene:)\s*",
    re.IGNORECASE,
)

_SUMMARY_SYSTEM = (
    "You turn one page of a book into a single illustratable moment. "
    "Choose the one visual beat that best represents the page - a concrete action "
    "or image, not a plot recap. Name every character who appears explicitly; never "
    "use 'he', 'she', or 'they' on their own. State what each character is doing, "
    "their expression and posture, and the location. Write 2-3 plain present-tense "
    "sentences, concrete and visual. Output only the description, with no preamble."
)

_IMAGE_PROMPT_SYSTEM = (
    "You write prompts for an SDXL-class text-to-image model. Rewrite the page "
    "moment into ONE image prompt. Rules:\n"
    "- Describe a single frozen moment: main subject(s) first, then their action, "
    "then the setting, then lighting and mood.\n"
    "- For every named character in the moment, inline their fixed traits from the "
    "Visual Bible (hair, age, build, clothing, colours). The model has no memory "
    "between pages, so repeat these every time or the character changes appearance.\n"
    "- Use concrete comma-separated visual phrases, not prose, metaphor, or "
    "narration. No lettering or captions in the described image.\n"
    "- 30 to 60 words. Age-appropriate for a children's picture book.\n"
    "Output only the prompt."
)


def _clean_llm_text(raw: str) -> str:
    """Strip the wrapping an instruct model tends to add: preambles, quotes,
    markdown fences, list bullets."""
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    text = _MARKUP_PREFIX_RE.sub("", text).strip()
    text = text.strip("`").strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1].strip()
    text = re.sub(r"^[-*•]\s+", "", text)
    return text.strip()


def _first_sentences(text: str, count: int = 2, max_chars: int = 320) -> str:
    """Cheap deterministic fallback when the LLM returns nothing."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if not collapsed:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", collapsed)
    snippet = " ".join(sentences[:count]).strip()
    return (snippet[:max_chars].rsplit(" ", 1)[0] + "...") if len(snippet) > max_chars else snippet


def _bible_names(visual_bible: str) -> str:
    """Pull 'Name: traits' lines back into a compact inline phrase for the
    heuristic fallback prompt."""
    parts = []
    for line in (visual_bible or "").splitlines():
        line = line.lstrip("- ").strip()
        if ":" in line:
            name, traits = line.split(":", 1)
            parts.append(f"{name.strip()} ({traits.strip()})" if traits.strip() else name.strip())
    return ", ".join(parts)


def generate_page_summary(page_text: str, previous_summaries: str = "") -> str:
    """
    Condenses a page into a single illustratable moment. Always returns a
    non-empty string (falls back to the opening sentences of the page if the
    LLM call fails), so downstream continuity joins can't hit a NULL.
    """
    context = ""
    if previous_summaries and previous_summaries.strip():
        context = (
            "Recent pages showed (for visual continuity only, do not describe them):\n"
            f"{previous_summaries.strip()}\n\n"
        )
    prompt = (
        f"{context}Page text:\n\"\"\"\n{page_text.strip()}\n\"\"\"\n\n"
        "Describe the single moment to illustrate for this page."
    )
    result = _clean_llm_text(ollama_generate(prompt, model=OLLAMA_MODEL, system=_SUMMARY_SYSTEM))
    return result or _first_sentences(page_text) or "A quiet scene from the story."


def generate_illustration_prompt(page_summary: str, visual_bible: str, scene: str = "") -> str:
    """
    Composes the final image-generation prompt from the page moment and the
    Visual Bible. Prepends the configured visual style. Never returns just the
    style string: if the LLM fails, it assembles a usable prompt from the
    summary and character traits directly.
    """
    style_key = os.getenv("IMAGE_STYLE", "storybook")
    style_desc = VISUAL_STYLES.get(style_key, VISUAL_STYLES["storybook"])["style"]

    scene_clean = (scene or "").strip()
    scene_line = ""
    if scene_clean and scene_clean.lower() not in _SCENE_NOISE:
        scene_line = f"SCENE SETTING: {scene_clean}\n\n"

    prompt = (
        f"VISUAL BIBLE (fixed character appearances - reuse exactly):\n"
        f"{visual_bible or 'No characters identified.'}\n\n"
        f"PAGE MOMENT:\n{page_summary}\n\n"
        f"{scene_line}"
        "Write the image prompt for this moment."
    )
    response = _clean_llm_text(ollama_generate(prompt, model=OLLAMA_MODEL, system=_IMAGE_PROMPT_SYSTEM))

    if not response:
        # LLM unavailable - build something renderable rather than degrading to
        # a bare style string that produces a generic, character-less image.
        names = _bible_names(visual_bible)
        moment = _first_sentences(page_summary, count=2) or "a scene from the story"
        response = ", ".join(p for p in (moment, names, scene_clean if scene_line else "") if p)

    return f"{style_desc}, {response}"
