"""Run GLM-OCR first and compare independent local evidence when needed."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

from config.settings import Settings
from ocr import engine as tess, glm, paddle
from ocr.quality import validate_recognition
from ocr.recognition import Recognition, RecognitionError
from utils.textutil import ISO_TO_TESS, REQUIRED_LANGUAGES, language_profile, norm_cmp


class OCRRouter:
    def __init__(self, settings: Settings, evidence: Path):
        self.settings = settings
        self.evidence = evidence
        self.paddle = paddle.PaddleOCR(settings)
        self.glm_status = None
        self.tess_packs = None

    def recognise(self, page, language: str, dpi: int, diag: dict,
                  status: Callable[[str], None]) -> tuple[Recognition | None, dict]:
        page_number = page.number + 1
        attempts = []
        observed = {}
        provisional = None
        for engine in ("glm-ocr", "paddleocr", "tesseract"):
            status(engine)
            repeats = 1 + min(2, max(0, self.settings.glm_ocr_retries)) if engine == "glm-ocr" else 1
            for _ in range(repeats):
                started = time.monotonic()
                result = None
                failure_status = "rejected"
                try:
                    if engine == "glm-ocr":
                        if self.glm_status is None:
                            self.glm_status = glm.model_info(self.settings)
                        if not self.glm_status["ready"]:
                            raise RecognitionError(self.glm_status["reason"], unavailable=True)
                        result = glm.recognise(page, dpi, self.settings)
                    elif engine == "paddleocr":
                        if language == "und" and observed.get("glm-ocr"):
                            language = language_profile(observed["glm-ocr"])["language"]
                        result = self.paddle.recognise(page, language, dpi)
                    else:
                        result = self._tesseract(page, language, dpi, diag, observed.get("glm-ocr", ""))
                    issues = validate_recognition(result, page_number, diag, self.settings)
                    observed[engine] = result.text()
                    expected = diag.get("native_language", {})
                    detected = language_profile(result.text(), language)
                    if (diag.get("health") in {"healthy", "suspicious"} and expected.get("confidence", 0) >= .6
                            and expected.get("script") not in detected["scripts"]):
                        issues.append("OCR lost the script detected in the native PDF text.")
                except RecognitionError as error:
                    issues = [str(error)]
                    failure_status = error.status
                except (OSError, ValueError, RuntimeError, TypeError, KeyError, IndexError):
                    issues = [f"{engine} returned an invalid result or stopped."]
                model = self.settings.glm_ocr_model if engine == "glm-ocr" else None
                if result is not None:
                    model = result.raw.get("model") or model
                attempt = {"engine": engine, "model": model, "status": "accepted" if not issues else failure_status,
                           "issues": issues, "seconds": round(time.monotonic() - started, 2)}
                attempts.append(attempt)
                if result is not None:
                    try:
                        self._save(result, len(attempts))
                    except OSError:
                        attempt["evidence_error"] = "Could not write the raw OCR response to the run folder."
                if result is not None and not issues:
                    profile = language_profile(result.text(), language)
                    info = {"engine": engine, "status": "accepted", "attempts": attempts,
                            "confidence": result.confidence, "requires_review": False,
                            "model": model, "language": profile}
                    if engine == "tesseract":
                        info.update({key: result.raw.get(key) for key in ("rotate", "skew", "langs")})
                    moderate_language = profile["language"] != "und" and profile["confidence"] < .6
                    if engine == "glm-ocr" and (profile["direction"] in {"mixed", "rtl"} or moderate_language):
                        provisional = result, {**info, "requires_review": True,
                                               "review_reason": "OCR language or direction requires independent verification."}
                        break
                    if engine != "glm-ocr" and observed.get("glm-ocr"):
                        from rapidfuzz import fuzz
                        a, b = observed["glm-ocr"], result.text()
                        similarity = fuzz.ratio(norm_cmp(a), norm_cmp(b))
                        info["fallback_similarity"] = round(similarity / 100, 3)
                        if (info["fallback_similarity"] < self.settings.ocr_min_fallback_similarity
                                or re.findall(r"\d+", a) != re.findall(r"\d+", b)):
                            info["requires_review"] = True
                            info["review_reason"] = "Local recognizers disagree on text, order, or numerical values."
                            if provisional:
                                provisional[1].update(fallback_similarity=info["fallback_similarity"],
                                                      review_reason=info["review_reason"])
                                continue
                    if result.confidence is not None and result.confidence < 80:
                        info.update(requires_review=True, review_reason="OCR confidence requires corroboration.")
                    return result, info
                if failure_status == "unavailable":
                    break
        if provisional:
            return provisional
        return None, {"engine": None, "status": "review_required", "attempts": attempts, "requires_review": True}

    def _tesseract(self, page, language, dpi, diagnostics, glm_text):
        if self.tess_packs is None:
            self.tess_packs = tess.available_langs(self.settings)
        if language == "und" and glm_text:
            profile = language_profile(glm_text)
            language = profile["language"]
            if language == "und" and profile["script"] == "arab":
                language = "ar"  # Independent Arabic-script trial. The result still requires review.
        if language == "und" and "osd" in self.tess_packs and diagnostics.get("image_coverage", 0) >= 0.4:
            _, script, confidence = tess.detect_orientation(tess.render_gray(page, min(dpi, 155)), self.settings)
            language = {"Arabic": "ar", "Japanese": "ja", "Hangul": "ko"}.get(script, "und") if confidence >= 2 else "und"
        if language not in REQUIRED_LANGUAGES or ISO_TO_TESS[language] not in self.tess_packs:
            raise RecognitionError("No independently tested Tesseract pack matches this page.", unavailable=True)
        lines, raw = tess.ocr_page(page, language, dpi, self.settings, self.tess_packs)
        confidence = sum(line.conf for line in lines) / len(lines) if lines else None
        return Recognition(page.number + 1, "tesseract", lines=lines, raw={**raw, "model": ISO_TO_TESS[language]},
                           confidence=confidence)

    def _save(self, result: Recognition, attempt: int) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / f"page-{result.page:04d}-{attempt}-{result.engine}.json"
        path.write_text(json.dumps({"page": result.page, "engine": result.engine, "raw": result.raw},
                                   ensure_ascii=False), encoding="utf-8")

    def close(self) -> None:
        self.paddle.close()
