import base64

import pymupdf

from config.settings import Settings
from ocr import glm, paddle, paddle_vl
from ocr.router import OCRRouter


def test_text_only_paddle_tag_is_detected_and_strict_mode_stops(tmp_path, monkeypatch):
    settings = Settings(ocr_backend="paddleocr-vl")
    def request(url, path, data, timeout):
        if path == "/api/tags":
            return {"models": [{"name": settings.paddle_vl_model, "digest": "fixture"}]}
        assert path == "/api/show"
        return {"capabilities": ["completion"]}
    monkeypatch.setattr(paddle_vl.ollama, "request_json", request)
    router = OCRRouter(settings, tmp_path)
    with pymupdf.open() as pdf:
        result, info = router.recognise(pdf.new_page(), "en", 175, {}, lambda _: None)
    assert result is None and info["requires_review"]
    assert [attempt["engine"] for attempt in info["attempts"]] == ["paddleocr-vl"]
    assert "vision projector" in info["attempts"][0]["issues"][0]


def test_vision_adapter_encodes_a_png_and_keeps_model_provenance(monkeypatch):
    settings = Settings()
    def request(url, path, data, timeout, **kwargs):
        assert path == "/api/generate" and data["prompt"] == "OCR:"
        assert data["model"] == "MedAIBase/PaddleOCR-VL:0.9b"
        assert base64.b64decode(data["images"][0]).startswith(b"\x89PNG")
        return {"model": data["model"], "done": True, "response": "Known inspection number 42."}
    monkeypatch.setattr(paddle_vl.ollama, "request_json", request)
    with pymupdf.open() as pdf:
        result = paddle_vl.recognise(pdf.new_page(), 175, settings)
    assert result.text() == "Known inspection number 42."
    assert result.engine == result.blocks[0].ocr_engine == "paddleocr-vl"
    assert result.confidence is None


def test_automatic_preserves_two_unavailable_reasons_before_python(tmp_path, monkeypatch):
    settings = Settings()
    monkeypatch.setattr(glm, "model_info", lambda _: {"ready": False, "reason": "Ollama is unavailable."})
    monkeypatch.setattr(paddle_vl, "model_info", lambda _: {"ready": False, "reason": "No vision projector."})
    def recognise(*_):
        return paddle.parse(1, [["The pressure remained stable."], [.97],
                                [[[5, 10], [200, 10], [200, 25], [5, 25]]]], 144, "local-rec")
    monkeypatch.setattr(paddle.PaddleOCR, "recognise", recognise)
    router, events = OCRRouter(settings, tmp_path), []
    with pymupdf.open() as pdf:
        result, info = router.recognise(pdf.new_page(), "en", 175, {}, lambda _: None, event=events.append)
    assert result.engine == "paddleocr" and info["selected_engine"] == "automatic"
    assert [attempt["engine"] for attempt in info["attempts"]] == ["glm-ocr", "paddleocr-vl", "paddleocr"]
    assert "No vision projector" in info["fallback_reason"]
    assert [item["engine"] for item in events if item["status"] == "switching"] == ["paddleocr-vl", "paddleocr"]
