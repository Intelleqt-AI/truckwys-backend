"""Language detection/normalization shared by the voice and typed-text quote-chat
paths, plus on-the-fly translation of the assistant's fixed English templates.

Two sources of a "detected language" feed the rest of the AI-quote pipeline:
- Voice: OpenAI's hosted Whisper endpoint returns a best-guess language NAME
  (e.g. "afrikaans"), not a code, and exposes no confidence score at all —
  normalize_whisper_language() maps that name to a code, treating any
  recognized name as authoritative (see AIVoiceQuoteView).
- Typed text: there is no transcription step, so detect_text_language() runs
  a dedicated statistical detector (langdetect) with both a probability
  threshold AND a minimum text length — langdetect is well known to return a
  wrong language at very high confidence on short strings (e.g. "hi" scores
  ~1.0 for Swahili), so probability alone is not a safe filter.

Both funnel into the same downstream contract: a code or None. None always
means "uncertain" -> the caller keeps its pre-existing default behavior; a
language is never invented.
"""
import hashlib
import logging
import os
from typing import Optional

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

try:
    from langdetect import detect_langs, DetectorFactory, LangDetectException
    # langdetect's classifier samples internally; without a fixed seed, the
    # same text can score differently across process restarts. Pinned once,
    # at import time, so detection is deterministic like everything else here.
    DetectorFactory.seed = 0
    LANGDETECT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    LANGDETECT_AVAILABLE = False

# OpenAI Whisper's documented supported-language list (name -> ISO 639-1 code,
# or the closest common code Whisper itself uses for a few languages that have
# no ISO 639-1 code). Keys are lowercase to match verbose_json's `.language`.
_WHISPER_CODE_TO_NAME = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish", "ru": "russian", "ko": "korean",
    "fr": "french", "ja": "japanese", "pt": "portuguese", "tr": "turkish", "pl": "polish", "ca": "catalan",
    "nl": "dutch", "ar": "arabic", "sv": "swedish", "it": "italian", "id": "indonesian", "hi": "hindi",
    "fi": "finnish", "vi": "vietnamese", "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay",
    "cs": "czech", "ro": "romanian", "da": "danish", "hu": "hungarian", "ta": "tamil", "no": "norwegian",
    "th": "thai", "ur": "urdu", "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin",
    "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak", "te": "telugu", "fa": "persian",
    "lv": "latvian", "bn": "bengali", "sr": "serbian", "az": "azerbaijani", "sl": "slovenian",
    "kn": "kannada", "et": "estonian", "mk": "macedonian", "br": "breton", "eu": "basque", "is": "icelandic",
    "hy": "armenian", "ne": "nepali", "mn": "mongolian", "bs": "bosnian", "kk": "kazakh", "sq": "albanian",
    "sw": "swahili", "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala", "km": "khmer",
    "sn": "shona", "yo": "yoruba", "so": "somali", "af": "afrikaans", "oc": "occitan", "ka": "georgian",
    "be": "belarusian", "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic", "yi": "yiddish",
    "lo": "lao", "uz": "uzbek", "fo": "faroese", "ht": "haitian creole", "ps": "pashto", "tk": "turkmen",
    "nn": "norwegian nynorsk", "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar",
    "bo": "tibetan", "tl": "tagalog", "mg": "malagasy", "as": "assamese", "tt": "tatar", "haw": "hawaiian",
    "ln": "lingala", "ha": "hausa", "ba": "bashkir", "jw": "javanese", "su": "sundanese", "yue": "cantonese",
}
WHISPER_LANGUAGE_TO_ISO639_1 = {name: code for code, name in _WHISPER_CODE_TO_NAME.items()}

# Below this length, langdetect's own reported confidence is not trustworthy
# (short greetings/phrases routinely score >0.99 for a wrong language, e.g.
# "hi" -> Swahili). Chosen from live testing against this app's own short
# stock phrases ("hi", "pickup in Durban" both mis-fire below ~20 chars).
_MIN_TEXT_LENGTH_FOR_DETECTION = 20

_DEFAULT_CONFIDENCE_THRESHOLD = 0.95


def _confidence_threshold() -> float:
    raw = os.environ.get("LANGUAGE_DETECT_CONFIDENCE_THRESHOLD") or getattr(
        settings, "LANGUAGE_DETECT_CONFIDENCE_THRESHOLD", None)
    try:
        return float(raw) if raw is not None else _DEFAULT_CONFIDENCE_THRESHOLD
    except (TypeError, ValueError):
        return _DEFAULT_CONFIDENCE_THRESHOLD


def normalize_whisper_language(name: Optional[str]) -> Optional[str]:
    """Map Whisper's returned language NAME (e.g. "afrikaans") to a code (e.g.
    "af"). None if `name` is falsy or not a language Whisper is documented to
    support — treated as "uncertain" by callers, never guessed."""
    if not name:
        return None
    return WHISPER_LANGUAGE_TO_ISO639_1.get(name.strip().lower())


def detect_text_language(text: str, threshold: Optional[float] = None) -> Optional[str]:
    """Best-effort language code for typed text with no transcription step.
    None (uncertain) when: langdetect isn't installed, the text is too short
    to trust langdetect's own confidence number, detection raises, or the
    top guess's probability is below `threshold` (default from
    LANGUAGE_DETECT_CONFIDENCE_THRESHOLD, else 0.95)."""
    text = (text or "").strip()
    if not LANGDETECT_AVAILABLE or len(text) < _MIN_TEXT_LENGTH_FOR_DETECTION:
        return None
    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return None
    if not candidates:
        return None
    top = candidates[0]
    if top.prob < (threshold if threshold is not None else _confidence_threshold()):
        return None
    return top.lang


def _cache_key(text: str, target_lang: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"tpl_translate:{target_lang}:{digest}"


def translate_template(text: str, target_lang: Optional[str]) -> str:
    """Translate a fixed/interpolated English reply string into `target_lang`
    (a code as produced by this module) via a small LLM call. Returns `text`
    unchanged when target_lang is falsy/'en', no LLM provider is configured,
    or the call fails for any reason — translation is always best-effort, and
    these code paths (deterministic replies, entity-creation dialogs, error
    messages) must keep working in English rather than ever raise or block."""
    if not text or not target_lang or target_lang == "en":
        return text

    key = _cache_key(text, target_lang)
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        from core.services import agent as agent_svc
        if not agent_svc._provider():
            return text
        system = (
            f"Translate the following text into the language with ISO 639-1 (or closest standard) "
            f"code '{target_lang}'. Return ONLY the translated text — no quotes, no explanations, no "
            f"extra commentary. Preserve any proper nouns, numbers, and placeholders exactly as given."
        )
        translated = agent_svc._llm_generate(system, [{"role": "user", "content": text}]).strip()
        if not translated:
            return text
        cache.set(key, translated, timeout=60 * 60 * 24 * 30)  # 30 days — fixed templates rarely change
        return translated
    except Exception:
        logger.warning("translate_template: failed to translate into %r, using English", target_lang, exc_info=True)
        return text
