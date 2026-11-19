"""Conservative OCR acceptance checks. A pass says nothing about factual correctness."""
from __future__ import annotations

import re
from collections import Counter

from config.settings import Settings
from diagnostics.page_diagnostics import text_health
from ocr.recognition import Recognition


def validate_recognition(result: Recognition, page: int, diagnostics: dict, settings: Settings) -> list[str]:
    issues = list(result.issues)
    if result.page != page or result.page < 1:
        issues.append("OCR response is missing the expected page reference.")
    text = result.text().strip()
    size = len(re.sub(r"\s+", "", text))
    if not size:
        return [*issues, "No OCR content was recognised."]
    if size < settings.ocr_min_chars or len(text) > settings.ocr_max_chars:
        issues.append("Recognised text length is outside the configured range.")
    if (diagnostics.get("health") == "corrupt" and diagnostics.get("native_chars", 0) > 100
            and size < diagnostics["native_chars"] * settings.ocr_min_native_ratio):
        issues.append("Recognised text may be incomplete compared with the visible text layer.")
    health = text_health(text)
    if health["ctrl"] + health["pua"] + health["moji"] / len(text) > settings.ocr_max_corruption_ratio:
        issues.append("Recognised text contains unusual control or replacement characters.")
    tokens = re.findall(r"\w+|[^\w\s]", text.casefold())
    windows = Counter(zip(*(tokens[offset:] for offset in range(6))))
    repeated = sum(count - 2 for count in windows.values() if count > 2)
    if len(tokens) >= 24 and repeated / len(tokens) > settings.ocr_max_repeat_ratio:
        issues.append("Recognised text contains excessive duplicated sequences.")
    if result.confidence is not None and result.engine == "paddleocr" and result.confidence < settings.ocr_min_paddle_confidence * 100:
        issues.append("Recognition scores are below the configured PaddleOCR threshold.")
    return issues
