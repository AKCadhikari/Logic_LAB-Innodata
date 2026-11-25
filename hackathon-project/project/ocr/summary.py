"""Summarize observed OCR runs separately from installation and model discovery."""
from collections import defaultdict

from ocr import glm, paddle, paddle_vl
from utils import ollama


def observed_runs(results):
    engines = defaultdict(lambda: {"requests": 0, "responses": 0, "failures": 0, "duration_ms": 0,
                                   "last_success": None, "last_error": None, "languages": {}})
    seen_ids = set()
    for result in results:
        for execution in reversed((result.get("execution_evidence") or {}).get("executions", [])):
            key = execution["execution_id"]
            if key in seen_ids or execution["mode"] == "MOCKED_TEST" or execution["task"] != "page_ocr":
                continue
            seen_ids.add(key)
            engine = execution.get("ocr_engine") or ("glm-ocr" if execution["provider"] == "ollama" else execution["provider"])
            row = engines[engine]
            row["requests"] += execution["request_status"] in {"attempted", "sent"}
            row["responses"] += execution["response_status"] == "received"
            row["duration_ms"] += execution["duration_ms"]
            language = execution.get("detected_language") or execution.get("language") or "und"
            if isinstance(language, dict):
                language = language.get("language", "und")
            group = row["languages"].setdefault(language, {"executions": 0, "failures": 0, "duration_ms": 0})
            group["executions"] += 1
            group["duration_ms"] += execution["duration_ms"]
            if execution["executed"] and execution["validation"] == "passed":
                if row["last_success"] is None:
                    row["last_success"] = {key: execution.get(key) for key in ("completed_at", "model", "model_digest")}
            else:
                row["failures"] += 1
                group["failures"] += 1
                if row["last_error"] is None:
                    row["last_error"] = {"message": (execution.get("error") or {}).get("message") or
                                         f"OCR {execution['validation']}", "at": execution["completed_at"]}
            if execution.get("worker_rss_bytes") is not None:
                row["worker_rss_bytes"] = max(row.get("worker_rss_bytes", 0), execution["worker_rss_bytes"])
        for page in result.get("page_log", []):
            for attempt in page.get("ocr", {}).get("attempts", []):
                if attempt["status"] != "accepted" and not attempt.get("execution_ids"):
                    row = engines[attempt["engine"]]
                    if row["last_error"] is None:
                        row["last_error"] = {"message": " ".join(attempt["issues"]), "at": result.get("finished")}
    return dict(engines)


def health(settings, runs, probes=None):
    from startup import find_ollama, loaded_models

    model = glm.model_info(settings)
    vl = paddle_vl.model_info(settings)
    configured_language = settings.paddle_ocr_lang or settings.paddle_ocr_unknown_lang or "en"
    local = paddle.readiness(settings, configured_language)
    resident = None
    if model["connected"]:
        try:
            resident = loaded_models(settings.glm_ocr_url)
        except ollama.OllamaError:
            pass
    target = ollama.tagged_model(settings.glm_ocr_model)
    vl_tag = ollama.tagged_model(settings.paddle_vl_model)
    vl_resident = resident if settings.paddle_vl_url == settings.glm_ocr_url else None
    if vl["connected"] and settings.paddle_vl_url != settings.glm_ocr_url:
        try:
            vl_resident = loaded_models(settings.paddle_vl_url)
        except ollama.OllamaError:
            pass
    engines = {
        "glm-ocr": {"installed": model["ready"] if model["connected"] else None,
                    "runtime_installed": bool(find_ollama(settings)), "server_online": model["connected"],
                    "available": model["ready"], "loaded": target in resident if resident is not None else None,
                    "residency": resident.get(target) if resident is not None else None,
                    "model": settings.glm_ocr_model, "digest": model.get("digest"), "reason": model["reason"]},
        "paddleocr-vl": {"installed": vl["installed"], "available": vl["ready"], "server_online": vl["connected"],
                         "runtime_installed": bool(find_ollama(settings)),
                         "loaded": vl_tag in vl_resident if vl_resident is not None else None,
                         "residency": vl_resident.get(vl_tag) if vl_resident is not None else None,
                         "model": vl["model"], "digest": vl["digest"], "reason": vl["reason"]},
        "paddleocr": {"installed": local.get("installed", False), "available": local.get("model_available", False),
                      "loaded": None, "model": local.get("model"), "reason": local["reason"],
                      "runtime_versions": local.get("versions", {}),
                      "languages": paddle.available_languages(settings), "configured_language": configured_language},
    }
    for name, entry in engines.items():
        seen = runs.get(name, {})
        success = seen.get("last_success")
        probe = (probes or {}).get(name)
        if probe and probe["verified"]:
            event = next((event for event in probe["executions"] if event["executed"]), None)
            if event and (not success or event["completed_at"] > success["completed_at"]):
                success = {key: event.get(key) for key in ("completed_at", "model", "model_digest")}
        digest = entry.get("digest")
        same_model = success and success["model"] == entry["model"] and (not digest or success["model_digest"] == digest)
        entry["last_success"] = success
        entry["last_error"] = seen.get("last_error")
        entry["probe"] = probe
        if probe and probe["error"]:
            entry["last_error"] = {"message": probe["error"], "at": probe["completed_at"]}
        entry["status"] = ("unavailable" if not entry["available"] or (name == "paddleocr" and not entry["installed"]) else
                           "verified_execution" if same_model else "available_unverified")
    return engines
