from dataclasses import replace

import pymupdf
import pytest

from config.settings import Settings
from ocr import glm, paddle
from ocr.recognition import RecognitionError
from ocr.router import OCRRouter
from ocr.summary import observed_runs


def glm_result(text):
    return glm.parse(1, {"done": True, "model": "glm-ocr:q8_0", "response": text})


def paddle_result(text, score=.97):
    return paddle.parse(1, [[text], [score], [[[10, 12], [240, 12], [240, 28], [10, 28]]]], 144, "latin-fixture")


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "empty"])
def test_strict_glm_records_failures_without_running_paddle(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(glm, "model_info", lambda _: {"ready": failure != "unavailable", "reason": "Model absent."})
    def recognise(*args):
        if failure == "timeout":
            raise RecognitionError("Ollama inference timed out.", status="timeout")
        return glm_result("")
    monkeypatch.setattr(glm, "recognise", recognise)
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", lambda *args: pytest.fail("Strict GLM must stop after GLM."))
    router = OCRRouter(Settings(ocr_backend="glm-ocr"), tmp_path)
    with pymupdf.open() as pdf:
        output, debug = router.recognise(pdf.new_page(), "en", 180, {}, lambda _: None)
    assert output is None and debug["requires_review"]
    assert debug["selected_engine"] == "glm-ocr"
    assert [attempt["engine"] for attempt in debug["attempts"]] == ["glm-ocr"]


def test_paddle_only_uses_job_language_and_never_contacts_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(glm, "model_info", lambda _: pytest.fail("Strict Paddle must not contact Ollama."))
    languages = []
    def recognise(self, page, language, dpi):
        languages.append(language)
        return paddle_result("Pump pressure remained within tolerance.")
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", recognise)
    router = OCRRouter(Settings(ocr_backend="paddleocr", paddle_ocr_unknown_lang="fr"), tmp_path)
    with pymupdf.open() as pdf:
        output, debug = router.recognise(pdf.new_page(), "und", 180, {}, lambda _: None)
    assert languages == ["fr"]
    assert output.engine == debug["selected_engine"] == "paddleocr"
    assert output.duration_ms is not None and output.lines[0].bbox


def test_reversed_automatic_primary_falls_back_when_paddle_unavailable(tmp_path, monkeypatch):
    def unavailable(*args):
        raise RecognitionError("Paddle weights are missing.", unavailable=True)
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", unavailable)
    monkeypatch.setattr(glm, "model_info", lambda _: {"ready": True})
    monkeypatch.setattr(glm, "recognise", lambda *args: glm_result("The system recorded pressure of 42 kPa."))
    router = OCRRouter(Settings(ocr_automatic_primary="paddleocr", ocr_automatic_secondary="glm-ocr"), tmp_path)
    with pymupdf.open() as pdf:
        output, debug = router.recognise(pdf.new_page(), "en", 180, {}, lambda _: None)
    assert output.engine == "glm-ocr" and debug["selected_engine"] == "automatic"
    assert "weights are missing" in debug["fallback_reason"]
    assert [attempt["engine"] for attempt in debug["attempts"]] == ["paddleocr", "glm-ocr"]


def test_automatic_conflict_preserves_one_result_and_requires_review(tmp_path, monkeypatch):
    text = "The system recorded pressure of 42 kPa."
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", lambda *args: paddle_result(text, .70))
    monkeypatch.setattr(glm, "model_info", lambda _: {"ready": True})
    monkeypatch.setattr(glm, "recognise", lambda *args: glm_result(text.replace("42", "12")))
    router = OCRRouter(Settings(ocr_automatic_primary="paddleocr", ocr_automatic_secondary="glm-ocr"), tmp_path)
    with pymupdf.open() as pdf:
        output, debug = router.recognise(pdf.new_page(), "en", 180, {}, lambda _: None)
    assert output.text() == text and debug["requires_review"]
    assert "disagree" in debug["review_reason"]


def test_metrics_exclude_test_and_replayed_executions():
    base = {"task": "page_ocr", "provider": "ollama", "ocr_engine": "glm-ocr", "executed": True,
            "validation": "passed", "model": "glm-ocr:q8_0", "duration_ms": 71,
            "request_status": "sent", "response_status": "received", "completed_at": "2026-01-01"}
    live = {**base, "execution_id": "real", "mode": "DOCUMENT_PROCESSING"}
    mocked = {**base, "execution_id": "mock", "mode": "MOCKED_TEST"}
    results = [{"execution_evidence": {"executions": [live, mocked]}},
               {"execution_evidence": {"executions": [live]}}]
    row = observed_runs(results)["glm-ocr"]
    assert row["requests"] == 1 and row["duration_ms"] == 71 and row["last_success"]["model"] == "glm-ocr:q8_0"
