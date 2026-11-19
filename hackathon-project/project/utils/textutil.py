"""Script / language heuristics and text helpers. Fully offline, no model downloads."""
from __future__ import annotations
import re
import unicodedata
from collections import Counter

ISO_TO_NAME = {"en": "english", "de": "german", "nl": "dutch", "it": "italian", "fr": "french",
               "cs": "czech", "sk": "slovak", "ar": "arabic", "ja": "japanese", "ko": "korean", "und": "unknown"}
ISO_TO_TESS = {"en": "eng", "de": "deu", "nl": "nld", "it": "ita", "fr": "fra", "cs": "ces", "sk": "slk",
               "ar": "ara", "ja": "jpn", "ko": "kor"}

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


def script_of(ch: str) -> str | None:
    o = ord(ch)
    if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
        return "arab"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
        return "han"
    if ch.isalpha():
        return "latin"
    return None


def script_counts(text: str) -> Counter:
    return Counter(s for s in (script_of(c) for c in text) if s)


def detect_lang(text: str, default: str = "und") -> str:
    sc = script_counts(text)
    total = sum(sc.values())
    if total < 8:
        return default
    if sc["arab"] / total > 0.3:
        return "ar"
    if sc["hangul"] / total > 0.15:
        return "ko"
    if sc["kana"] / total > 0.05 or sc["han"] / total > 0.3:
        return "ja"
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 5:
        return default
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
    if scores[best] <= 0.04 or (len(words) < 40 and scores[best] - scores[second] < 0.01):
        return default
    return best


def lang_attr(code: str, fmt: str = "iso") -> str:
    return ISO_TO_NAME.get(code, code) if fmt == "name" else code


def is_rtl(text: str) -> bool:
    sc = script_counts(text)
    t = sum(sc.values())
    return t > 0 and sc["arab"] / t > 0.4


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
    return bool(_NONLATIN.search(s))
