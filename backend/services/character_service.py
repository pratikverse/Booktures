import os
import json
import logging
import re
from typing import Dict, List, Optional
import spacy
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from services.pdf_service import ollama_generate
from models import Character, DocumentChunk, page_characters

logger = logging.getLogger(__name__)


class CharacterExtractionError(RuntimeError):
    """Raised when character extraction can't produce a usable result.

    The job worker turns this into a failed job with a visible status_note
    instead of quietly finishing a book that has an empty Visual Bible.
    """


try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    logger.warning("spaCy model 'en_core_web_sm' not found. Character extraction will be limited.")
    nlp = None

ALIAS_MATCH_SCORE = 85
OLLAMA_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
NO_VISUAL_PROFILE = "No visual description available."

# Character extraction reads the whole book at once, so it benefits from a
# large-context model. Default to Gemini (2.5 Flash free tier ~1M-token context)
# regardless of the global LLM_PROVIDER; set CHARACTER_LLM_PROVIDER="" to just
# use LLM_PROVIDER. Needs GEMINI_API_KEY when set to "gemini".
CHARACTER_LLM_PROVIDER = os.getenv("CHARACTER_LLM_PROVIDER", "gemini").strip().lower() or None
# How much of the book to send in one pass. Small-context providers (groq/ollama)
# fall back to the sampled excerpt below; large-context ones get this many chars.
CHARACTER_LLM_MAX_CHARS = int(os.getenv("CHARACTER_LLM_MAX_CHARS", "120000"))


def _character_generate(prompt: str, system: str = "") -> str:
    """LLM call for character work: tries CHARACTER_LLM_PROVIDER, then falls
    back to the default provider if it yields nothing (e.g. missing API key)."""
    result = ollama_generate(prompt, model=OLLAMA_MODEL, system=system, provider=CHARACTER_LLM_PROVIDER)
    if not result.strip() and CHARACTER_LLM_PROVIDER:
        result = ollama_generate(prompt, model=OLLAMA_MODEL, system=system)
    return result

HONORIFIC_RE = re.compile(r"^(mr|mrs|ms|miss|dr|prof|sir|lady|lord)\.?\s+", re.I)


def process_book_characters(book_id: int, db: Session):
    """
    Full pipeline to identify, normalize, and profile characters in a book.
    CHARACTER_EXTRACTION_MODE=llm uses a single LLM pass instead of spaCy NER
    (useful on API-only/no-GPU deploys where the transformer NER model isn't installed).

    Raises CharacterExtractionError if it can't produce any characters, so the
    job fails visibly instead of completing with an empty Visual Bible. Override
    with CHARACTER_EXTRACTION_ALLOW_EMPTY=true for texts with no named characters.
    """
    chunks = db.query(DocumentChunk).filter(DocumentChunk.book_id == book_id).order_by(DocumentChunk.page_number).all()
    full_text = " ".join(c.content for c in chunks)

    if not full_text.strip():
        raise CharacterExtractionError("No extracted text to analyze for characters.")

    mode = os.getenv("CHARACTER_EXTRACTION_MODE", "spacy").strip().lower()
    if mode == "llm":
        _process_characters_llm(book_id, chunks, full_text, db)
    elif not nlp:
        raise CharacterExtractionError(
            "spaCy model 'en_core_web_sm' is not installed and "
            "CHARACTER_EXTRACTION_MODE is not 'llm'. Install the model or set "
            "CHARACTER_EXTRACTION_MODE=llm."
        )
    else:
        _process_characters_spacy(book_id, chunks, full_text, db)

    found = db.query(Character).filter(Character.book_id == book_id).count()
    if found == 0 and os.getenv("CHARACTER_EXTRACTION_ALLOW_EMPTY", "false").strip().lower() != "true":
        raise CharacterExtractionError(
            f"Character extraction (mode={mode}) found no characters. The Visual "
            "Bible would be empty and every illustration would be off-model. Check "
            "the LLM provider / API key, or set CHARACTER_EXTRACTION_ALLOW_EMPTY=true "
            "to proceed anyway (e.g. for a text with no named characters)."
        )
    logger.info("Character extraction (mode=%s) persisted %d characters for book %d", mode, found, book_id)


def _process_characters_spacy(book_id: int, chunks: List[DocumentChunk], full_text: str, db: Session):
    raw_mentions: Dict[str, List[int]] = {}
    for chunk in chunks:
        doc = nlp(chunk.content)
        for ent in doc.ents:
            if ent.label_ == "PERSON":
                name = ent.text.strip()
                if len(name) > 2 and not _is_noise(name):
                    raw_mentions.setdefault(name, []).append(chunk.id)

    canonical_groups = _group_aliases(raw_mentions)

    for main_name, group in canonical_groups.items():
        context = _gather_character_context(full_text, main_name)
        visual_profile = _extract_visual_traits_llm(main_name, context)

        char_obj = Character(
            book_id=book_id,
            name=main_name,
            aliases=", ".join(sorted(group["aliases"])),
            visual_profile=visual_profile,
            mention_count=group["total_mentions"],
        )
        db.add(char_obj)
        db.flush()

        for chunk_id in set(group["chunk_ids"]):
            db.execute(page_characters.insert().values(character_id=char_obj.id, chunk_id=chunk_id))

    db.commit()


def _process_characters_llm(book_id: int, chunks: List[DocumentChunk], full_text: str, db: Session):
    characters = _extract_characters_llm(full_text)
    for c in characters:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        aliases = sorted({name, *[a.strip() for a in c.get("aliases", []) if a and a.strip()]})
        visual_profile = (c.get("visual_profile") or "").strip() or NO_VISUAL_PROFILE
        chunk_ids = _find_chunk_ids(chunks, aliases)

        char_obj = Character(
            book_id=book_id,
            name=name,
            aliases=", ".join(aliases),
            visual_profile=visual_profile,
            mention_count=len(chunk_ids),
        )
        db.add(char_obj)
        db.flush()

        for chunk_id in set(chunk_ids):
            db.execute(page_characters.insert().values(character_id=char_obj.id, chunk_id=chunk_id))

    db.commit()


def _extract_characters_llm(full_text: str) -> List[Dict]:
    """Single-pass LLM character + visual trait extraction, no spaCy required."""
    # A large-context provider (Gemini) can read most of the book in one pass;
    # otherwise fall back to a spread-out excerpt so late characters aren't missed.
    if CHARACTER_LLM_PROVIDER == "gemini":
        sample = full_text[:CHARACTER_LLM_MAX_CHARS]
    else:
        sample = full_text[:4000]
        if len(full_text) > 8000:
            mid = len(full_text) // 2
            sample += "\n...\n" + full_text[mid:mid + 2000] + "\n...\n" + full_text[-2000:]

    system = (
        "You are a character profiler. Identify main characters and their stable physical "
        "visual traits (hair, eyes, clothing, age). Respond ONLY with valid JSON, no commentary: "
        '[{"name": "...", "aliases": ["..."], "visual_profile": "one concise plain-text sentence, not nested JSON"}]'
    )
    for _ in range(2):
        response = _character_generate(sample, system=system)
        data = _safe_parse_json(response)
        if isinstance(data, list):
            return data
    logger.warning("LLM character extraction failed after retry; no characters found.")
    return []


def _find_chunk_ids(chunks: List[DocumentChunk], names: List[str]) -> List[int]:
    ids = []
    lowered = [n.lower() for n in names if n]
    for chunk in chunks:
        text_lower = chunk.content.lower()
        if any(n in text_lower for n in lowered):
            ids.append(chunk.id)
    return ids


def _is_noise(name: str) -> bool:
    noise_words = {"author", "project gutenberg", "chapter", "illustration", "page"}
    return any(word in name.lower() for word in noise_words)


def _normalize_name(name: str) -> str:
    return HONORIFIC_RE.sub("", name).strip()


def _group_aliases(mentions: Dict[str, List[int]]) -> Dict[str, Dict]:
    """Union-find alias grouping (order-independent), honorific-insensitive."""
    names = list(mentions.keys())
    parent = {n: n for n in names}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    normalized = {n: _normalize_name(n) for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if fuzz.partial_ratio(normalized[a], normalized[b]) > ALIAS_MATCH_SCORE:
                union(a, b)

    groups: Dict[str, Dict] = {}
    for n in names:
        root = find(n)
        g = groups.setdefault(root, {"aliases": set(), "chunk_ids": [], "total_mentions": 0})
        g["aliases"].add(n)
        g["chunk_ids"].extend(mentions[n])
        g["total_mentions"] += len(mentions[n])

    # Key each group by its most-mentioned alias for a readable canonical name.
    return {max(g["aliases"], key=lambda a: len(mentions[a])): g for g in groups.values()}


def _gather_character_context(text: str, name: str, window: int = 200, max_snippets: int = 10) -> str:
    """Samples mentions spread across the whole book, not just the first few."""
    mentions = [m.start() for m in re.finditer(re.escape(name), text)]
    if not mentions:
        return ""
    if len(mentions) <= max_snippets:
        picks = mentions
    else:
        step = len(mentions) / max_snippets
        picks = [mentions[int(i * step)] for i in range(max_snippets)]
    snippets = [text[max(0, s - window):min(len(text), s + window)] for s in picks]
    return "... ".join(snippets)


def _extract_visual_traits_llm(name: str, context: str) -> str:
    if not context.strip():
        logger.warning(f"No context found for character '{name}'; using fallback description.")
        return NO_VISUAL_PROFILE

    prompt = f"Extract stable physical visual traits for character '{name}' from context:\n\n{context}"
    system = "Describe physical traits (hair, age, clothing). Be highly concise."

    result = _character_generate(prompt, system=system)
    if not result.strip():
        result = _character_generate(prompt, system=system)  # one retry
    if not result.strip():
        logger.warning(f"LLM returned no visual profile for '{name}' after retry.")
        return NO_VISUAL_PROFILE
    return result


def extract_page_metadata(page_text: str) -> Dict[str, str]:
    """Extracts key characters and scene description from a single page's text, as JSON."""
    prompt = (
        "Identify characters present and the primary scene/setting in this text. "
        'Respond ONLY with valid JSON in this exact shape: '
        '{"characters": ["name1", "name2"], "scene": "short description"}'
    )
    for _ in range(2):
        response = ollama_generate(f"{prompt}\n\nText: {page_text[:2000]}", model=OLLAMA_MODEL)
        data = _safe_parse_json(response)
        if isinstance(data, dict):
            chars = ", ".join(data.get("characters") or []) or "Unknown"
            scene = (data.get("scene") or "").strip() or "Standard setting"
            return {"characters": chars, "scene": scene}

    logger.warning("Failed to parse page metadata JSON after retry; using fallback.")
    return {"characters": "Unknown", "scene": "Standard setting"}


def _safe_parse_json(raw: str):
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return None
