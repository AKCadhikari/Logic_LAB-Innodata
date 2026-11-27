"""Per-page diagnostics. Never trust the native text layer blindly: score it, then decide native / ocr / compare."""
from __future__ import annotations
import re
from collections import Counter
from utils.textutil import language_profile

_VOWELS = set("aeiouyáéíóúýäöüàèìòùâêîôûåæøœāēīōūěůi")
_MOJI = re.compile(r"Ã[\x80-\xbf¡-¿]|â€|Â[\xa0-\xbf]|ï¿½|Ð[\x80-\xbf]")


def _wordlike(tok: str) -> bool:
    t = tok.lower()
    if len(t) < 3:
        return True
    if not any(c in _VOWELS for c in t):
        return False
    run = 0
    for c in t:
        run = 0 if c in _VOWELS else run + 1
        if run >= 5:
            return False
    return True


def text_health(text: str) -> dict:
    n = len(text)
    if n == 0:
        return {"chars": 0, "ctrl": 0.0, "pua": 0.0, "moji": 0, "wordlike": 1.0, "latin_tokens": 0}
    ctrl = sum(1 for c in text if (ord(c) < 32 and c not in "\t\n\r") or 0x7F <= ord(c) <= 0x9F)
    pua = sum(1 for c in text if 0xE000 <= ord(c) <= 0xF8FF or ord(c) in (0xFFFD, 0xFFFE, 0xFFFF))
    toks = [t for t in re.findall(r"[^\W\d_]+", text) if all(ord(c) < 0x250 for c in t)]
    wl = sum(_wordlike(t) for t in toks) / len(toks) if toks else 1.0
    return {"chars": n, "ctrl": ctrl / n, "pua": pua / n, "moji": len(_MOJI.findall(text)),
            "wordlike": wl, "latin_tokens": len(toks)}


def analyze_page(page, settings, metadata_language: str = "und") -> dict:
    import pymupdf

    # Use the same Unicode policy as styled extraction. Raw glyph IDs can look
    # like valid characters and hide missing mappings during diagnostics.
    flags = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_USE_CID_FOR_UNKNOWN_UNICODE
    text = page.get_text("text", flags=flags) or ""
    stripped = text.strip()
    h = text_health(stripped)
    missing_unicode = stripped.count("\ufffd")
    area = max(page.rect.width * page.rect.height, 1.0)
    try:
        imgs = page.get_image_info()
    except Exception:
        imgs = []
    cov = min(1.0, sum(max(0.0, (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1])) for i in imgs) / area)
    language = language_profile(stripped, metadata_language)
    shaped_glyphs = sum(0xFB50 <= ord(char) <= 0xFDFF or 0xFE70 <= ord(char) < 0xFEFF for char in stripped)
    bidi_risk = language["direction"] == "mixed" and any(
        language_profile(line)["direction"] == "mixed" for line in stripped.splitlines())
    decoding_risk = shaped_glyphs >= 3 or bidi_risk
    reasons, decision, health = [], "native", "healthy"
    if not stripped or (len(stripped) < settings.native_min_chars and cov >= 0.2):
        health, decision = ("absent", "ocr")
        reasons.append(f"only {len(stripped)} native chars" + (f", image coverage {cov:.0%}" if imgs else ""))
    elif h["ctrl"] > settings.native_ctrl_ratio_corrupt:
        health, decision = "corrupt", "ocr"; reasons.append(f"control-character ratio {h['ctrl']:.0%}")
    elif h["pua"] > 0.2:
        health, decision = "corrupt", "ocr"; reasons.append(f"private-use glyph ratio {h['pua']:.0%} (legacy symbol-font encoding)")
    elif missing_unicode:
        health, decision = "corrupt", "ocr"
        reasons.append(f"The PDF text layer contains {missing_unicode} unmapped Unicode characters.")
    elif h["moji"] >= 6:
        health, decision = "corrupt", "ocr"; reasons.append(f"{h['moji']} mojibake sequences")
    elif h["latin_tokens"] >= 40 and h["wordlike"] < 0.55:
        health, decision = "corrupt", "ocr"; reasons.append(f"only {h['wordlike']:.0%} of tokens look like words")
    else:
        sus = []
        if h["pua"] > settings.native_pua_ratio_suspicious:
            sus.append(f"private-use/replacement chars {h['pua']:.1%}")
        if h["ctrl"] > 0.002:
            sus.append(f"control chars {h['ctrl']:.1%}")
        if h["latin_tokens"] >= 40 and h["wordlike"] < 0.8:
            sus.append(f"word-likeness {h['wordlike']:.0%}")
        if h["moji"]:
            sus.append(f"{h['moji']} mojibake sequences")
        if shaped_glyphs >= 3:
            sus.append("The PDF exposes shaped glyphs. Logical Unicode and mixed-direction order require verification.")
        if bidi_risk:
            sus.append("Opposing text directions share a native line. Verify logical order, punctuation and numbers against the source.")
        if sus:
            health = "suspicious"; decision = "compare" if settings.compare_suspicious else "native"; reasons += sus
    mixed = bool(stripped) and cov >= settings.native_mixed_min_image_coverage and len(stripped) <= settings.native_mixed_max_chars
    if mixed and decision != "ocr":
        decision = "ocr"
        reasons.append("A large image accompanies a sparse native text layer.")
    return {
        "page": page.number + 1, "width": page.rect.width, "height": page.rect.height,
        "rotation": page.rotation, "native_chars": h["chars"], "text_density": round(h["chars"] / area * 1e4, 2),
        "has_native_text": bool(stripped), "short_native_text": len(stripped) < settings.native_min_chars,
        "has_images": bool(imgs), "appears_scanned": (not stripped and cov >= 0.4) or mixed,
        "mixed_content": mixed,
        "ocr_required": decision in ("ocr", "compare"),
        "ctrl_ratio": round(h["ctrl"], 4), "pua_ratio": round(h["pua"], 4), "mojibake": h["moji"],
        "wordlike": round(h["wordlike"], 3), "n_images": len(imgs), "image_coverage": round(cov, 3),
        "scripts": language["scripts"], "health": health, "decision": decision, "reasons": reasons,
        "native_language": language,
        "native_unmapped_characters": missing_unicode,
        "native_decoding_risk": decoding_risk,
        "lang_hint": language["language"] if health == "healthy" or (
            missing_unicode and language["script"] == "arab" and language["confidence"] >= .6) else "und",
    }
