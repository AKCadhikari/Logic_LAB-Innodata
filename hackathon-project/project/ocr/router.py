"""Route a page through its job's selected OCR engines."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

from config.settings import Settings
from ocr import glm, paddle, paddle_vl
from ocr.quality import validate_recognition
from ocr.recognition import ENGINE_MODES, OCR_ENGINES, Recognition, RecognitionError
from utils.textutil import language_profile, norm_cmp
from utils import execution_audit as audit


class OCRRouter:
    def __init__(self, settings: Settings, evidence: Path):
        self.settings = settings
        self.evidence = evidence
        self.paddle = paddle.PaddleOCR(settings)
        self.model_status = {}
        mode = settings.ocr_backend
        order = (settings.ocr_automatic_primary, settings.ocr_automatic_secondary, settings.ocr_automatic_tertiary)
        if mode not in ENGINE_MODES or (mode == "automatic" and (
                any(engine not in OCR_ENGINES for engine in order[:2]) or (order[2] and order[2] not in OCR_ENGINES))):
            raise ValueError("Invalid OCR engine selection or Automatic fallback configuration.")
        self.engines = tuple(dict.fromkeys(filter(None, order))) if mode == "automatic" else (mode,)
        self.ollama_engines = {"glm-ocr": glm, paddle_vl.ENGINE: paddle_vl}
        self.models = {"glm-ocr": settings.glm_ocr_model, paddle_vl.ENGINE: settings.paddle_vl_model}

    def recognise(self, page, language: str, dpi: int, diag: dict,
                  status: Callable[[str], None], *, event: Callable[[dict], None] | None = None,
                  probe: bool = False) -> tuple[Recognition | None, dict]:
        page_number = page.number + 1
        attempts = []
        observed = ""
        provisional = None
        for engine_index, engine in enumerate(self.engines):
            fallback_reason = None
            if engine_index:
                previous = attempts[-1]
                fallback_reason = (provisional[1]["review_reason"] if provisional else
                                   f"{previous['engine']} {previous['status']}: " + " ".join(previous["issues"]))
            status(engine)
            model = self.models.get(engine)
            if event and fallback_reason:
                event({"engine": engine, "model": model, "status": "switching", "fallback_reason": fallback_reason})
            repeats = 1 + min(2, max(0, self.settings.glm_ocr_retries)) if engine == "glm-ocr" else 1
            for _ in range(repeats):
                started = time.monotonic()
                result = None
                failure_status = "rejected"
                input_language = language
                inference_attempted = False
                audit_start = audit.cursor()
                try:
                    with audit.details(selected_ocr_engine=self.settings.ocr_backend, ocr_engine=engine,
                                       fallback_reason=fallback_reason):
                        adapter = self.ollama_engines.get(engine)
                        if adapter:
                            if engine not in self.model_status:
                                self.model_status[engine] = adapter.model_info(self.settings)
                            state = self.model_status[engine]
                            # The synthetic probe can confirm the server's explicit image rejection.
                            if not state["ready"] and not (probe and engine == paddle_vl.ENGINE and state.get("installed")):
                                raise RecognitionError(state["reason"], unavailable=True)
                            inference_attempted = True
                            with audit.details(model_digest=state.get("digest")):
                                result = adapter.recognise(page, dpi, self.settings)
                        else:
                            input_language = self.paddle_language(language, observed, diag)
                            if event:
                                model = paddle.models(self.settings, input_language)[1].name
                                event({"engine": engine, "model": model, "status": "requesting",
                                       "fallback_reason": fallback_reason})
                            result = self.paddle.recognise(page, input_language, dpi)
                    issues = validate_recognition(result, page_number, diag, self.settings)
                    observed = result.text()
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
                if result is not None:
                    model = result.model or model
                attempt = {"engine": engine, "model": model, "status": "accepted" if not issues else failure_status,
                           "issues": issues, "seconds": round(time.monotonic() - started, 2),
                           "input_language": input_language}
                if fallback_reason:
                    attempt["fallback_reason"] = fallback_reason
                ids = audit.ids_since(audit_start)
                profile = language_profile(result.text(), input_language) if result is not None else {}
                audit.mark(ids, validation="rejected" if issues else "passed", validation_issue_count=len(issues),
                           detected_language=profile.get("language"))
                if ids:
                    attempt["execution_ids"] = ids
                    attempt["inference_ms"] = round(sum(item["duration_ms"] for item in audit.since(audit_start)), 2)
                if result is not None and not result.execution_ids:
                    result.execution_ids = ids
                if engine in self.ollama_engines:
                    attempt["document_inference_attempted"] = inference_attempted
                attempts.append(attempt)
                if event:
                    event(attempt)
                if result is not None and self.settings.audit_private_responses:
                    try:
                        self._save(result, len(attempts))
                    except OSError:
                        attempt["evidence_error"] = "Could not write the raw OCR response to the run folder."
                if result is not None and not issues:
                    result.language, result.duration_ms = profile, round((time.monotonic() - started) * 1000, 2)
                    info = {"engine": engine, "status": "accepted", "attempts": attempts,
                            "confidence": result.confidence, "requires_review": False,
                            "model": model, "language": profile, "selected_execution_ids": result.execution_ids,
                            "selected_engine": self.settings.ocr_backend, "fallback_reason": fallback_reason}
                    moderate_language = profile["language"] != "und" and profile["confidence"] < .6
                    if engine in self.ollama_engines and (profile["direction"] in {"mixed", "rtl"} or moderate_language):
                        info.update(requires_review=True, review_reason="OCR language or direction requires independent verification.")
                    if result.confidence is not None and result.confidence < 80:
                        info.update(requires_review=True, review_reason="OCR confidence requires corroboration.")
                    if info.get("review_reason"):
                        result.warnings.append(info["review_reason"])
                    if provisional:
                        from rapidfuzz import fuzz
                        a, b = provisional[0].text(), result.text()
                        similarity = fuzz.ratio(norm_cmp(a), norm_cmp(b))
                        info["fallback_similarity"] = round(similarity / 100, 3)
                        if (info["fallback_similarity"] < self.settings.ocr_min_fallback_similarity
                                or re.findall(r"\d+", a) != re.findall(r"\d+", b)):
                            provisional[1].update(fallback_similarity=info["fallback_similarity"], fallback_reason=fallback_reason,
                                                  review_reason="Local recognizers disagree on text, order, or numerical values.")
                            if engine_index < len(self.engines) - 1:
                                break
                            return provisional
                    if info["requires_review"] and engine_index < len(self.engines) - 1:
                        provisional = provisional or (result, info)
                        break
                    return result, info
                if failure_status == "unavailable":
                    break
        if provisional:
            provisional[1]["fallback_reason"] = fallback_reason
            return provisional
        return None, {"engine": None, "status": "review_required", "attempts": attempts, "requires_review": True,
                      "selected_engine": self.settings.ocr_backend, "fallback_reason": fallback_reason}

    def paddle_language(self, language, observed, diagnostics):
        if self.settings.paddle_ocr_lang or language != "und":
            return self.settings.paddle_ocr_lang or language
        profile = language_profile(observed) if observed else diagnostics.get("native_language", {})
        if profile.get("language") not in {None, "und", "mul"}:
            return profile["language"]
        script = profile.get("script")
        if script == "arab":
            return "ar"
        if script not in {None, "und", "latin"}:
            return "und"
        return self.settings.paddle_ocr_unknown_lang or "und"

    def _save(self, result: Recognition, attempt: int) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / f"page-{result.page:04d}-{attempt}-{result.engine}.json"
        path.write_text(json.dumps({"page": result.page, "engine": result.engine, "raw": result.raw},
                                   ensure_ascii=False), encoding="utf-8")

    def close(self) -> None:
        self.paddle.close()
