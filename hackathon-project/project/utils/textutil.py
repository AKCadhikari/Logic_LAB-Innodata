"""Script / language heuristics and text helpers. Fully offline, no model downloads."""
from __future__ import annotations
import re
import unicodedata
from collections import Counter
from functools import lru_cache

import regex

ISO_TO_NAME = {"en": "english", "de": "german", "nl": "dutch", "it": "italian", "fr": "french",
               "cs": "czech", "sk": "slovak", "ar": "arabic", "ja": "japanese", "ko": "korean", "und": "unknown"}
ISO_TO_TESS = {"en": "eng", "de": "deu", "nl": "nld", "it": "ita", "fr": "fra", "cs": "ces", "sk": "slk",
               "ar": "ara", "ja": "jpn", "ko": "kor", "fa": "fas", "ur": "urd", "he": "heb",
               "hi": "hin", "si": "sin", "ta": "tam", "zh": "chi_sim"}
ISO_TO_NAME.update(fa="persian", ur="urdu", he="hebrew", hi="hindi", si="sinhala", ta="tamil", zh="chinese", mul="multiple")
REQUIRED_LANGUAGES = ("ar", "ja", "ko", "cs", "sk", "de", "nl", "it", "fr", "en")
LANGUAGE_SCRIPTS = {**{key: "latin" for key in ("en", "de", "nl", "it", "fr", "cs", "sk")},
                    "ar": "arab", "fa": "arab", "ur": "arab", "he": "hebrew", "hi": "devanagari",
                    "si": "sinhala", "ta": "tamil", "zh": "han", "ja": "kana", "ko": "hangul"}
_SCRIPTS = regex.compile("|".join(f"(?P<{key}>\\p{{Script={value}}})" for key, value in (
    ("latin", "Latin"), ("arab", "Arabic"), ("hebrew", "Hebrew"), ("devanagari", "Devanagari"),
    ("sinhala", "Sinhala"), ("tamil", "Tamil"), ("han", "Han"), ("hangul", "Hangul"),
    ("hiragana", "Hiragana"), ("katakana", "Katakana"),
)))

_STOP = {
    "en": "the of and to in is that for with as by on are this be from or an at which it was not have has can".split(),
    "de": "der die und in den von zu das mit sich des auf für ist im dem nicht ein eine als auch es an werden aus er hat".split(),
    "nl": "de het een en van in is dat op te zijn voor met die niet aan er als ook door om bij naar of".split(),
    "it": "il di che la e in un per è con non una sono del dei le si da come più alla della nel gli anche".split(),
    "fr": "le la les de des du et en un une est que pour dans qui par sur au avec ne pas plus se ce sont ou".split(),
    "cs": "a se na je že s v o z do pro to jako by ale po od jsou nebo který byl být také při".split(),
    "sk": "a sa na je že s v o z do pre to ako by ale po od sú alebo ktorý bol byť tiež pri".split(),
}
_DIAC = {"cs": "ěščřžýáíéúůďťň", "sk": "äĺľôŕšžčťňďúýáíé", "de": "äöüß", "fr": "éèêàçùâîôû", "it": "àèéìòù", "nl": "ëï"}


@lru_cache(maxsize=4096)
def script_of(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    match = _SCRIPTS.fullmatch(ch)
    if not match:
        return None if not regex.fullmatch(r"\P{Script=Common}", ch) else "other"
    return "kana" if match.lastgroup in {"hiragana", "katakana"} else match.lastgroup


def script_counts(text: str) -> Counter:
    return Counter(s for s in (script_of(c) for c in text) if s)


def _latin_language(text: str) -> tuple[str, float]:
    words = [word for word in re.findall(r"[^\W\d_]+", text.casefold())
             if all(script_of(letter) == "latin" for letter in word)]
    if len(words) < 4:
        return "und", 0.0
    cnt = Counter(words)
    scores = {}
    for lg, stop in _STOP.items():
        s = sum(cnt[w] for w in stop) / len(words)
        d = _DIAC.get(lg)
        if d:
            s += 0.8 * sum(text.lower().count(c) for c in d) / max(len(text), 1)
        scores[lg] = s
    ranked = sorted(scores, key=scores.get, reverse=True)
    best, second = ranked[0], ranked[1]
    margin = scores[best] - scores[second]
    if scores[best] <= 0.04 or margin < 0.01:
        return "und", 0.0
    return best, round(min(0.92, 0.48 + 2 * margin + min(len(words), 40) / 200), 3)


def direction_of(text: str) -> tuple[str, str]:
    """Paragraph base direction from UAX #9 first strong character, excluding isolates."""
    depth = 0
    directions = set()
    base = "und"
    for char in text:
        kind = unicodedata.bidirectional(char)
        if kind in {"LRI", "RLI", "FSI"}:
            depth += 1
        elif kind == "PDI":
            depth = max(0, depth - 1)
        elif kind in {"L", "R", "AL"}:
            direction = "ltr" if kind == "L" else "rtl"
            directions.add(direction)
            if not depth and base == "und":
                base = direction
    return base, "mixed" if len(directions) > 1 else base


def language_profile(text: str, hint: str = "und") -> dict:
    scripts = script_counts(text)
    base, direction = direction_of(text)
    dominant = scripts.most_common(1)[0][0] if scripts else "und"
    # Han is shared by Japanese and Chinese. Kana or trusted PDF metadata can identify Japanese.
    if dominant == "han" and scripts["kana"]:
        dominant = "kana"
    elif dominant == "han" and scripts["hangul"]:
        dominant = "hangul"
    language, confidence, method = "und", 0.0, "unicode-script"
    if dominant == "latin":
        language, confidence = _latin_language(text)
        method = "local-stopwords"
    elif dominant == "arab":
        if any(char in text for char in "ٹڈڑںےۓھ"):
            language, confidence = "ur", 0.86
        elif any(char in text for char in "گچپژکی") and not any(char in text for char in "ٹڈڑںے"):
            language, confidence = "fa", 0.67
        elif len(re.findall(r"\b(?:هذا|هذه|التي|الذي|من|في|على|إلى|اللغة|العربية)\b", text)) >= 2:
            language, confidence = "ar", 0.78
    elif dominant in {"kana", "hangul", "hebrew", "sinhala", "tamil"}:
        language = next(code for code, script in LANGUAGE_SCRIPTS.items() if script == dominant)
        confidence = 0.95
    if language == "und" and (LANGUAGE_SCRIPTS.get(hint) == dominant
                              or (dominant == "han" and hint == "ja")):
        language, confidence, method = hint, 0.6, "compatible-metadata-hint"
    return {"language": language, "script": dominant, "scripts": dict(scripts), "direction": direction,
            "base_direction": base, "confidence": confidence, "method": method,
            "review_required": bool(scripts.get("other") or (sum(scripts.values()) >= 12 and language == "und"))}


def detect_lang(text: str, default: str = "und") -> str:
    return language_profile(text, default)["language"]


def lang_attr(code: str, fmt: str = "iso") -> str:
    return ISO_TO_NAME.get(code, code) if fmt == "name" else code


def is_rtl(text: str) -> bool:
    return direction_of(text)[0] == "rtl"


def xml_safe(s: str) -> str:
    """Keep only characters that are legal in XML 1.0."""
    out = []
    for ch in s:
        o = ord(ch)
        if o in (0x9, 0xA, 0xD) or 0x20 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF:
            out.append(ch)
    return "".join(out)


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def norm_cmp(s: str) -> str:
    """Normalisation used for comparing texts (NFKC, case-folded, whitespace collapsed)."""
    return norm_ws(unicodedata.normalize("NFKC", s)).casefold()


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def join_words(words: list[str]) -> str:
    """Join OCR words; no space between adjacent CJK/Hangul glyph tokens."""
    out = ""
    for w in words:
        if not out:
            out = w
        elif _CJK.search(out[-1]) and _CJK.search(w[:1]):
            out += w
        else:
            out += " " + w
    return out


_LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def fix_ligatures(s: str) -> str:
    return "".join(_LIG.get(c, c) for c in s) if any(c in _LIG for c in s) else s


_NONLATIN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u1100-\u11ff]")


def has_nonlatin(s: str) -> bool:
    return any(script != "latin" for script in script_counts(s))
