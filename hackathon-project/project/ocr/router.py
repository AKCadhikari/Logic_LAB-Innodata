"""Run GLM-OCR first, escalating to PaddleOCR only after a failed attempt."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from config.settings import Settings
from ocr import glm, paddle
from ocr.quality import validate_recognition
from ocr.recognition import Recognition, RecognitionError


class OCRRouter:
    def __init__(self, settings: Settings, evidence: Path):
        self.settings = settings
        self.evidence = evidence
        self.paddle = paddle.PaddleOCR(settings)
        self.glm_status = None

    def recognise(self, page, language: str, dpi: int, diag: dict,
                  status: Callable[[str], None]) -> tuple[Recognition | None, dict]:
        page_number = page.number + 1
        attempts = []
        observed = {}
        for engine in ("glm-ocr", "paddleocr"):
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
                    else:
                        result = self.paddle.recognise(page, language, dpi)
                    issues = validate_recognition(result, page_number, diag, self.settings)
                    observed[engine] = result.text()
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
                    info = {"engine": engine, "status": "accepted", "attempts": attempts,
                            "confidence": result.confidence, "requires_review": False,
                            "model": model}
                    if engine == "paddleocr" and observed.get("glm-ocr"):
                        from rapidfuzz import fuzz
                        from utils.textutil import norm_cmp
                        a, b = observed["glm-ocr"], result.text()
                        similarity = fuzz.ratio(norm_cmp(a), norm_cmp(b))
                        info["fallback_similarity"] = round(similarity / 100, 3)
                        if info["fallback_similarity"] < self.settings.ocr_min_fallback_similarity:
                            info["requires_review"] = True
                            info["review_reason"] = "GLM-OCR and the PaddleOCR fallback disagree."
                    return result, info
                if failure_status == "unavailable":
                    break
        return None, {"engine": None, "status": "review_required", "attempts": attempts, "requires_review": True}

    def _save(self, result: Recognition, attempt: int) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / f"page-{result.page:04d}-{attempt}-{result.engine}.json"
        path.write_text(json.dumps({"page": result.page, "engine": result.engine, "raw": result.raw},
                                   ensure_ascii=False), encoding="utf-8")

    def close(self) -> None:
        self.paddle.close()
