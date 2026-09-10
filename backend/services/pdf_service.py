"""
PDF Service: Manages the lifecycle of PDF files and provides high-fidelity text extraction.
It focuses on identifying narrative text while filtering out structural noise like headers and footers.
Ollama integration adds LLM-backed quality evaluation, header/footer classification, and OCR repair.
"""

import os
import uuid
import json
import pdfplumber
import httpx
import pytesseract
from collections import Counter
from dotenv import load_dotenv
from functools import lru_cache
import logging
import re
from typing import Any
from providers.llm_provider import get_llm_provider

load_dotenv()

logger = logging.getLogger(__name__)

# Allow overriding the Tesseract binary path via environment variable
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# ---------------------------------------------------------------------------
# Ollama Configuration
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
OLLAMA_FAST_MODEL = os.getenv("OLLAMA_FAST_MODEL", "phi3")

OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180.0"))
OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "True").lower() == "true"


def _llm_enabled(provider: str | None = None) -> bool:
    """Whether LLM-backed enrichment (summaries, OCR repair, classification,
    character traits) should run.

    OLLAMA_ENABLED only gates *Ollama* - a cloud provider (Groq/Gemini) is
    independent of it. So enrichment is on when a cloud provider is selected,
    or when Ollama is explicitly enabled.
    """
    effective = (provider or os.getenv("LLM_PROVIDER", "ollama")).strip().lower()
    return effective != "ollama" or OLLAMA_ENABLED

# ---------------------------------------------------------------------------
# Regex Patterns for Identifying Metadata and Noise
# ---------------------------------------------------------------------------
ROMAN_NUMERAL_PATTERN = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
# Matches common page number formats: "Page 1", "pg. 4", or standalone numbers/roman numerals
PAGE_COUNTER_PATTERN = re.compile(
    r"^(page|pg)\.?\s*[\divxlcdm]+$|^[\divxlcdm]+$",
    re.IGNORECASE,
)
SECTION_HEADING_PATTERN = re.compile(
    r"^(chapter|chap|part|book|section|prologue|epilogue)\b[\w\s:.-]*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Heuristic Thresholds for Text Quality and Cleaning
# ---------------------------------------------------------------------------
MAX_HEADER_FOOTER_LINE_LENGTH = 100
MIN_REPEAT_COUNT = 3
MIN_REPEAT_RATIO = 0.25
OCR_MIN_LENGTH_THRESHOLD = 160
OCR_MIN_TOKEN_THRESHOLD = 30

# ---------------------------------------------------------------------------
# OCR Engine Configuration
# ---------------------------------------------------------------------------
# 200 DPI is enough for Tesseract on body text and roughly halves the bitmap
# memory vs 300 - matters on a 512MB cloud instance. Override with OCR_DPI.
OCR_RENDER_RESOLUTION = int(os.getenv("OCR_DPI", "200"))
OCR_TESSERACT_CONFIG = "--oem 3 --psm 6"

# Cap the LLM OCR-repair work so a long scanned book can't exhaust a cloud
# provider's rate limit: at most N chunks repaired per page, and a process-wide
# budget across the whole run. Both overridable via env.
OCR_REPAIR_MAX_CHUNKS_PER_PAGE = int(os.getenv("OCR_REPAIR_MAX_CHUNKS_PER_PAGE", "3"))
OCR_REPAIR_MAX_CALLS = int(os.getenv("OCR_REPAIR_MAX_CALLS", "40"))
_ocr_repair_calls = 0
LIGATURE_REPLACEMENTS = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
}

# ---------------------------------------------------------------------------
# Ollama System Prompts
# ---------------------------------------------------------------------------
_QUALITY_SYSTEM_PROMPT = (
    "You are a document quality evaluator. "
    "Respond with JSON only, no extra text, no markdown: "
    "{\"weak\": true/false, \"reason\": \"brief reason\"}"
)

_HEADER_FOOTER_SYSTEM_PROMPT = (
    "You classify document lines. "
    "Respond with JSON only, no extra text, no markdown: "
    "{\"is_noise\": true/false}"
)

_OCR_REPAIR_SYSTEM_PROMPT = (
    "You repair OCR-extracted text from books and documents. "
    "Fix broken words, ligature errors, misread characters, and spacing issues. "
    "Preserve the original meaning and structure exactly. "
    "Return only the repaired text, no explanations, no extra commentary."
)


# ===========================================================================
# Ollama Client
# ===========================================================================

def ollama_generate(prompt: str, model: str = None, system: str = "", provider: str = None) -> str:
    """
    Runs a single LLM completion through the configured provider (Ollama, Groq,
    or Gemini). Named for historical reasons - it is not Ollama-specific.

    Args:
        prompt:   The user prompt to send.
        model:    Ollama model tag (e.g. "phi3", "qwen2.5:7b"). Ignored for
                  non-Ollama providers, which use their own configured default.
        system:   Optional system prompt to set model behaviour.
        provider: Force a specific provider ("gemini", "groq", "ollama")
                  regardless of LLM_PROVIDER. Used by character extraction to
                  reach Gemini's large context window.

    Returns:
        The model's response string, or "" on any failure.
    """
    effective = (provider or os.getenv("LLM_PROVIDER", "ollama")).strip().lower()
    if not _llm_enabled(effective):
        return ""
    if effective == "ollama":
        # Honour a model chosen live in Settings over the caller's frozen
        # module-constant hint; fall back to that hint, then a sane default.
        model = os.getenv("OLLAMA_DEFAULT_MODEL") or model or "qwen2.5:7b"
    else:
        # Model tags like "phi3"/"qwen2.5:7b" are Ollama-specific; other
        # providers fall back to their own configured default instead of 404ing.
        model = None
    return get_llm_provider(provider).generate(prompt, system=system, model=model)


def _safe_parse_json(raw: str, fallback: dict) -> dict:
    """
    Attempts to parse a JSON string returned by Ollama.
    Strips markdown fences if present and returns fallback on failure.
    """
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.debug("Failed to parse Ollama JSON response: %r", raw)
        return fallback


# ===========================================================================
# File Management
# ===========================================================================

def save_pdf(file_bytes: bytes, filename: str) -> str:
    """
    Saves a PDF via the configured storage provider (local disk by default,
    Nhost/R2 in deployments with an ephemeral filesystem - same
    abstraction illustrations already use, so uploaded PDFs survive a
    backend restart instead of 404ing once local disk gets wiped).

    Args:
        file_bytes: The raw binary data of the PDF.
        filename:   The original name of the file.

    Returns:
        A relative path (local storage) or absolute URL (Nhost/R2)
        identifying the saved file.
    """
    from providers.storage_provider import get_storage_provider

    unique_name = f"{uuid.uuid4()}_{filename}"
    key = f"pdfs/{unique_name}"
    saved = get_storage_provider().save(file_bytes, key, content_type="application/pdf")
    if not saved:
        raise RuntimeError("Failed to save PDF via the configured storage provider.")
    return saved


# ===========================================================================
# Main Extraction Pipeline
# ===========================================================================

def extract_text_by_page(pdf_path: str) -> list[dict]:
    """
    Main entry point for text extraction.  Iterates through pages, scores
    content, performs OCR if needed, repairs OCR output with Ollama, and
    removes running headers/footers.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        A list of dicts with keys:
            page_number     – 1-based page index
            text            – cleaned, readable text
            weak_text       – bool; True if text quality is still suspect
            extraction_meta – dict with source, score, ocr_applied flags
    """
    pages: list[dict] = []
    header_counts: Counter = Counter()
    footer_counts: Counter = Counter()

    # book.file_path is a remote URL when STORAGE_PROVIDER isn't local (e.g.
    # Nhost) - pdfplumber and PyMuPDF need actual bytes, so fetch once and hand
    # pdfplumber a fresh BytesIO (the stream can't be read twice).
    if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
        from io import BytesIO
        response = httpx.get(pdf_path, timeout=60.0)
        response.raise_for_status()
        pdf_source_bytes = response.content
        plumber_source: Any = BytesIO(pdf_source_bytes)
    else:
        plumber_source = pdf_path

    # PyMuPDF (fitz): fastest and usually the cleanest digital extraction. Used
    # as an extra candidate alongside pdfplumber, still subject to the same
    # quality scoring so a bad page can't win by default.
    pymupdf_texts: list[str] = []
    try:
        import fitz  # PyMuPDF
        if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
            fitz_doc = fitz.open(stream=pdf_source_bytes, filetype="pdf")
        else:
            fitz_doc = fitz.open(pdf_path)
        try:
            pymupdf_texts = [page.get_text("text") or "" for page in fitz_doc]
        finally:
            fitz_doc.close()
    except Exception as exc:
        logger.debug("PyMuPDF unavailable for %s: %s", pdf_path, exc)

    # ------------------------------------------------------------------
    # Phase 1: Initial extraction and header/footer candidate collection
    # ------------------------------------------------------------------
    with pdfplumber.open(plumber_source) as pdf:
        for i, page in enumerate(pdf.pages):
            pymupdf_text = pymupdf_texts[i] if i < len(pymupdf_texts) else None
            raw_text, extraction_meta = _extract_page_text(page, pymupdf_text=pymupdf_text)
            # Break text into lines to identify repeating structural elements
            lines = _split_lines(raw_text)

            if lines:
                # Track top/bottom lines across the whole book to find common headers/footers
                for line in lines[:2]:
                    header_counts[_normalize_running_line(line)] += 1
                for line in lines[-2:]:
                    footer_counts[_normalize_running_line(line)] += 1

            pages.append({
                "page_number": i + 1,
                "lines": lines,
                "raw_text": raw_text,
                "extraction_meta": extraction_meta,
            })

    # Identify lines that appear frequently enough to be considered noise
    header_candidates = _select_header_footer_candidates(header_counts, len(pages))
    footer_candidates = _select_header_footer_candidates(footer_counts, len(pages))

    # ------------------------------------------------------------------
    # Phase 2: Clean text by removing identified noise
    # ------------------------------------------------------------------
    cleaned_pages: list[dict] = []
    for page in pages:
        # Remove identified headers, footers, and page markers
        filtered_lines = _filter_page_lines(
            page["lines"],
            header_candidates,
            footer_candidates,
        )

        # Fallback: if filtering removed everything, keep original lines
        if not filtered_lines and page["lines"]:
            filtered_lines = page["lines"]

        cleaned_text = (
            # Handles line merging and de-hyphenation
            _reconstruct_text(filtered_lines)
            if filtered_lines
            else _normalize_text(page["raw_text"])
        )

        # Discard pages that contain only numbers or roman numerals
        if _is_page_marker_only_text(cleaned_text):
            cleaned_text = ""

        # Heuristic quality check first (fast)
        heuristic_weak = _is_weak_page_text(cleaned_text)

        # LLM quality check on borderline pages (slower but smarter)
        weak_text = _is_weak_page_text_llm(cleaned_text, heuristic_weak)

        cleaned_pages.append({
            "page_number": page["page_number"],
            "text": cleaned_text,
            "weak_text": weak_text,
            "extraction_meta": page.get("extraction_meta", {}),
        })

    return cleaned_pages


# ===========================================================================
# Page-Level Extraction
# ===========================================================================

def _extract_page_text(page, pymupdf_text=None) -> tuple[str, dict]:
    """
    Evaluates multiple extraction methods for a single page and chooses the
    best result.  Triggers OCR if digital extraction is poor, and then runs
    Ollama-based repair if OCR wins.
    """
    candidates: list[tuple[str, str]] = []

    # Method 0: PyMuPDF (fitz) - fastest, usually cleanest
    if pymupdf_text:
        candidates.append(("pymupdf", pymupdf_text))

    # Method 1: pdfplumber with layout preservation
    plumber_layout = page.extract_text(layout=True, x_tolerance=1, y_tolerance=1)
    if plumber_layout:
        candidates.append(("plumber_layout", plumber_layout))

    # Method 2: pdfplumber plain text
    plumber_plain = page.extract_text(x_tolerance=1, y_tolerance=1)
    if plumber_plain:
        candidates.append(("plumber_plain", plumber_plain))

    # Final fallback for digital extraction: extract individual words
    if not candidates:
        words = page.extract_words(use_text_flow=True)
        candidates.append(("plumber_words", " ".join(w["text"] for w in words)))

    # Select the best digital version based on heuristic scoring
    text, source, score = _pick_best_text_candidate(candidates)

    ocr_applied = False
    ocr_improved = False
    ocr_repaired = False
    final_score = score

    # If the best digital text is still unreadable/short, attempt OCR
    if _should_try_ocr(text):
        ocr_applied = True
        ocr_text = _extract_page_text_with_ocr(page)
        ocr_score = _extraction_score(ocr_text)

        if ocr_score > score:
            ocr_improved = True
            final_score = ocr_score
            source = "ocr"

            # Ollama post-processing: repair common OCR artefacts
            repaired = _repair_ocr_text(ocr_text)
            if repaired and repaired != ocr_text:
                ocr_repaired = True
                ocr_text = repaired

            return ocr_text, {
                "source": source,
                "score": round(final_score, 2),
                "ocr_applied": ocr_applied,
                "ocr_improved": ocr_improved,
                "ocr_repaired": ocr_repaired,
            }

    return text, {
        "source": source,
        "score": round(final_score, 2),
        "ocr_applied": ocr_applied,
        "ocr_improved": ocr_improved,
        "ocr_repaired": ocr_repaired,
    }


def _pick_best_text_candidate(candidates: list[tuple[str, str]]) -> tuple[str, str, float]:
    """
    Iterates through extraction sources and selects the one with the highest
    quality score.
    """
    best_text = ""
    best_source = "unknown"
    best_score = float("-inf")
    for source, candidate in candidates:
        normalized = _normalize_text(candidate or "")
        score = _extraction_score(normalized)
        if score > best_score:
            best_score = score
            best_text = normalized
            best_source = source
    return best_text, best_source, best_score


# ===========================================================================
# OCR
# ===========================================================================

def _extract_page_text_with_ocr(page) -> str:
    """
    Renders the PDF page as an image and runs Tesseract OCR.
    """
    try:
        page_image = page.to_image(resolution=OCR_RENDER_RESOLUTION).original
    except Exception as exc:
        logger.warning("OCR render (pdf-to-image) failed: %s", exc)
        return ""

    try:
        text = pytesseract.image_to_string(page_image, config=OCR_TESSERACT_CONFIG)
        return text or ""
    except Exception as exc:
        logger.error("Tesseract OCR binary failed or not in PATH: %s", exc)
        return ""


def _should_try_ocr(text: str) -> bool:
    """
    Determines if a page is a candidate for OCR based on text density,
    alphabetic ratio, and symbol density.
    """
    normalized = _normalize_text(text)
    if not normalized:
        return True

    tokens = normalized.split()
    if len(normalized) < OCR_MIN_LENGTH_THRESHOLD:
        return True
    if len(tokens) < OCR_MIN_TOKEN_THRESHOLD:
        return True

    alpha_tokens = sum(1 for t in tokens if re.search(r"[A-Za-z]{2,}", t))
    alpha_ratio = alpha_tokens / max(len(tokens), 1)
    symbol_ratio = (
        len(re.findall(r"[^A-Za-z0-9\s.,;:!?'\-\"()\[\]]", normalized))
        / max(len(normalized), 1)
    )
    if alpha_ratio < 0.45:
        return True
    return symbol_ratio > 0.08 and alpha_ratio < 0.6


# ===========================================================================
# Ollama-Powered Enhancements
# ===========================================================================

def _repair_ocr_text(text: str) -> str:
    """
    Uses Ollama to fix common OCR artefacts: broken words, missed spaces,
    misread characters, and residual ligature noise.

    Only called when OCR was the winning extraction source.
    Processes text in sentence-aware chunks to stay within context limits.
    Falls back silently to the original text on any failure.

    Args:
        text: Raw OCR-extracted text for one page.

    Returns:
        Repaired text string, or the original if repair failed / was skipped.
    """
    global _ocr_repair_calls

    if not _llm_enabled() or not text or len(text) < 50:
        return text

    # Break page text into chunks to prevent hitting LLM context limits
    chunks = _chunk_text_for_llm(text, max_chars=1200)
    repaired_chunks: list[str] = []

    for i, chunk in enumerate(chunks):
        over_page_cap = i >= OCR_REPAIR_MAX_CHUNKS_PER_PAGE
        over_run_budget = _ocr_repair_calls >= OCR_REPAIR_MAX_CALLS
        if over_page_cap or over_run_budget:
            # Leave the remaining chunks as raw OCR rather than spending more
            # (rate-limited) LLM calls on a single book.
            repaired_chunks.append(chunk)
            continue
        prompt = f"Repair this OCR-extracted text:\n\n{chunk}"
        result = ollama_generate(prompt, model=OLLAMA_DEFAULT_MODEL, system=_OCR_REPAIR_SYSTEM_PROMPT)
        _ocr_repair_calls += 1
        repaired_chunks.append(result if result else chunk)

    return " ".join(repaired_chunks)


def _is_weak_page_text_llm(text: str, heuristic_weak: bool) -> bool:
    """
    Uses Ollama to verify whether extracted page text is genuinely unreadable.

    Strategy:
      - If the fast heuristic says text is GOOD (heuristic_weak=False),
        trust it and skip the LLM call entirely.
      - If the heuristic says text is WEAK, ask Ollama for a second opinion
        to avoid false positives on technical/foreign-language content.

    Args:
        text:             Cleaned page text.
        heuristic_weak:   Result of the regex-based _is_weak_page_text().

    Returns:
        True if the page text is considered low quality, False otherwise.
    """
    if not _llm_enabled():
        return heuristic_weak

    # Heuristic is confident the text is fine — no need to call LLM
    if not heuristic_weak:
        return False

    if not text or len(text) < 80:
        return True

    # Send a sample of the text to the LLM for a readability second-opinion
    sample = text[:600]
    prompt = (
        "Is the following extracted PDF text garbled, mostly symbols, "
        "or unreadable gibberish? Evaluate overall readability.\n\n"
        f"Text:\n{sample}"
    )
    raw = ollama_generate(prompt, model=OLLAMA_DEFAULT_MODEL, system=_QUALITY_SYSTEM_PROMPT)
    result = _safe_parse_json(raw, fallback={"weak": heuristic_weak})
    return bool(result.get("weak", heuristic_weak))


@lru_cache(maxsize=512)
def _is_likely_running_header_footer_llm(line: str) -> bool:
    """
    LLM-backed classifier for header/footer noise detection.

    Uses an LRU cache (size 512) so repeated lines across hundreds of pages
    only trigger a single Ollama call.

    Falls back to regex heuristic if Ollama is disabled or returns garbage.

    Args:
        line: A single line of text from a page.

    Returns:
        True if the line appears to be structural noise (header/footer/page number).
    """
    if not _llm_enabled():
        return _is_likely_running_header_footer(line)

    # Fast-path: let obvious cases bypass the LLM
    if _looks_like_page_marker(line):
        return True
    if len(line) > MAX_HEADER_FOOTER_LINE_LENGTH:
        return False

    prompt = (
        f"Is this line a running page header, footer, page number, copyright notice, "
        f"or other structural/navigational noise in a book or document? "
        f"It should NOT be actual prose or meaningful content.\n\n"
        f"Line: \"{line}\""
    )
    raw = ollama_generate(prompt, model=OLLAMA_FAST_MODEL, system=_HEADER_FOOTER_SYSTEM_PROMPT)
    result = _safe_parse_json(raw, fallback={"is_noise": None})

    # If LLM gave no clear answer, fall back to regex heuristic
    if result.get("is_noise") is None:
        return _is_likely_running_header_footer(line)

    return bool(result["is_noise"])


# ===========================================================================
# Scoring
# ===========================================================================

def _extraction_score(text: str) -> float:
    """
    A heuristic scoring function. Rewards word count and readability.
    Penalises single-letter tokens, weird spacing, and broken hyphens.
    """
    if not text:
        return 0.0
    normalized = _normalize_text(text)
    if not normalized:
        return 0.0

    tokens = normalized.split()
    alpha_tokens = sum(1 for t in tokens if re.search(r"[A-Za-z]{2,}", t))
    words = re.findall(r"[A-Za-z]{2,}", normalized)
    bad_chars = len(re.findall(r"[^A-Za-z0-9\s.,;:!?'\-\"()\[\]]", normalized))
    bad_ratio = bad_chars / max(len(normalized), 1)
    single_letter_ratio = (
        sum(1 for t in tokens if len(t) == 1 and t.isalpha())
        / max(len(tokens), 1)
    )
    # Detect gaps like "t h i s" or "i n t e r- esting"
    weird_spacing_penalty = len(re.findall(r"\b[A-Za-z]\s+[A-Za-z]\b", normalized))
    broken_hyphen_penalty = len(re.findall(r"\w+-\s+\w", normalized))

    return (
        len(words) * 1.0
        + alpha_tokens * 0.5
        + min(len(normalized), 3000) * 0.02
        - bad_ratio * 50
        - single_letter_ratio * 80
        - weird_spacing_penalty * 0.8
        - broken_hyphen_penalty * 0.5
    )


# ===========================================================================
# Line / Text Utilities
# ===========================================================================

def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _select_header_footer_candidates(counter: Counter, total_pages: int) -> set[str]:
    """
    Filters a counter of lines to find those appearing frequently enough to be
    considered headers or footers.
    """
    threshold = max(MIN_REPEAT_COUNT, int(total_pages * MIN_REPEAT_RATIO))
    return {
        line
        for line, count in counter.items()
        if count >= threshold and _is_likely_running_header_footer_llm(line)
    }


def _normalize_running_line(line: str) -> str:
    """
    Normalises a line for header/footer comparison by replacing page numbers
    with a placeholder so "Page 1" and "Page 2" map to the same signature.
    """
    normalized = line.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\b\d+\b", "#", normalized)
    normalized = re.sub(r"\b[ivxlcdm]+\b", "#", normalized)
    return normalized


def _is_likely_running_header_footer(line: str) -> bool:
    """
    Regex-only fallback: applies heuristics to check if a line looks like
    structural noise (metadata). Used when Ollama is disabled or unavailable.
    """
    normalized = line.strip()
    if not normalized:
        return False
    if len(normalized) > MAX_HEADER_FOOTER_LINE_LENGTH:
        return False
    if _looks_like_page_marker(normalized):
        return True
    if SECTION_HEADING_PATTERN.match(normalized):
        return True
    words = normalized.split()
    if 1 <= len(words) <= 12 and normalized == normalized.upper():
        return True
    # Likely running headers such as "Book Title | Author Name"
    if 1 <= len(words) <= 10 and re.search(r"[|/:-]", normalized):
        return True
    return False


def _looks_like_page_marker(line: str) -> bool:
    """Checks if a line is just a page number or roman numeral."""
    stripped = line.strip().lower()
    if not stripped:
        return False
    if PAGE_COUNTER_PATTERN.fullmatch(stripped):
        return True
    if ROMAN_NUMERAL_PATTERN.fullmatch(stripped):
        return True
    if re.fullmatch(r"-\s*[\divxlcdm]+\s*-", stripped):
        return True
    return False


def _filter_page_lines(
    lines: list[str],
    headers: set[str],
    footers: set[str],
) -> list[str]:
    """
    Removes matching headers/footers from the start and end of the page lines
    list, and filters out standalone page numbers from the middle.
    """
    filtered = list(lines)

    while filtered and _normalize_running_line(filtered[0]) in headers:
        filtered.pop(0)
    while filtered and _normalize_running_line(filtered[-1]) in footers:
        filtered.pop()

    cleaned: list[str] = []
    for line in filtered:
        if _looks_like_page_marker(line):
            continue
        if len(line.split()) <= 1 and line.strip().isdigit():
            continue
        cleaned.append(line)

    return cleaned


def _reconstruct_text(lines: list[str]) -> str:
    """
    Joins lines back into a single string, handling de-hyphenation at line
    endings when the next line continues the word.
    """
    if not lines:
        return ""

    merged_lines: list[str] = []
    index = 0
    while index < len(lines):
        current = lines[index].strip()
        if not current:
            index += 1
            continue
        while (
            current.endswith("-")
            and index + 1 < len(lines)
            and lines[index + 1][:1].islower()
        ):
            current = current[:-1] + lines[index + 1].lstrip()
            index += 1
        merged_lines.append(current)
        index += 1

    return _normalize_text(" ".join(merged_lines))


def _normalize_text(text: str) -> str:
    """
    Final cleanup: fixes ligatures, collapses whitespace, removes trailing
    hyphens, and handles punctuation spacing.
    """
    if not text:
        return ""

    cleaned = text.strip()
    for source, target in LIGATURE_REPLACEMENTS.items():
        cleaned = cleaned.replace(source, target)
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Fix words split by line breaks: "inter- esting" -> "interesting"
    cleaned = re.sub(r"(?<=\w)-\s+(?=\w)", "", cleaned)
    # Remove space before punctuation
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    # Normalise bracket spacing
    cleaned = re.sub(r"([(\[{])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([)\]}])", r"\1", cleaned)
    # Heuristic: add space between lowercase and uppercase if stuck (OCR error)
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)

    return cleaned


def _is_page_marker_only_text(text: str) -> bool:
    """Checks if the entire text content is just page numbers/roman numerals."""
    if not text:
        return False
    lines = _split_lines(text)
    if not lines:
        return False
    return all(_looks_like_page_marker(line) for line in lines)


def _is_weak_page_text(text: str) -> bool:
    """
    Heuristic-only check. Evaluates if final cleaned text appears to be low
    quality. Called first; result is passed to _is_weak_page_text_llm().
    """
    normalized = _normalize_text(text)
    if not normalized:
        return True
    tokens = normalized.split()
    if len(normalized) < OCR_MIN_LENGTH_THRESHOLD:
        return True
    if len(tokens) < OCR_MIN_TOKEN_THRESHOLD:
        return True
    alpha_tokens = sum(1 for t in tokens if re.search(r"[A-Za-z]{2,}", t))
    alpha_ratio = alpha_tokens / max(len(tokens), 1)
    single_letter_ratio = (
        sum(1 for t in tokens if len(t) == 1 and t.isalpha())
        / max(len(tokens), 1)
    )
    return alpha_ratio < 0.45 or single_letter_ratio > 0.22


# ===========================================================================
# Helpers
# ===========================================================================

def _chunk_text_for_llm(text: str, max_chars: int = 1200) -> list[str]:
    """
    Splits text into sentence-aware chunks for LLM processing, ensuring each
    chunk stays within the max_chars budget.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
            current = sentence
        else:
            current += " " + sentence
    if current:
        chunks.append(current.strip())
    return chunks