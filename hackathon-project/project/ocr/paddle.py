"""Page-by-page PaddleOCR fallback with a bounded local CPU worker."""
from __future__ import annotations

import importlib.util
import multiprocessing
import os
import sys
import time
from pathlib import Path

from config.settings import Settings
from layout.model import Line
from ocr.engine import render_gray
from ocr.recognition import Recognition, RecognitionError
from utils.textutil import is_rtl

LATIN = {"en", "fr", "de", "nl", "it", "cs", "sk", "und", ""}
RECOGNIZERS = {"ja": "PP-OCRv5_mobile_rec", "zh": "PP-OCRv5_mobile_rec",
               "ko": "korean_PP-OCRv5_mobile_rec", "ar": "arabic_PP-OCRv3_mobile_rec"}


def models(settings: Settings, language: str) -> tuple[Path, Path]:
    language = settings.paddle_ocr_lang or language
    root = Path(sys.prefix) / "ocr-models"
    name = "latin_PP-OCRv5_mobile_rec" if language in LATIN else RECOGNIZERS.get(language, "")
    if not name and not settings.paddle_rec_model_dir:
        raise RecognitionError("PaddleOCR has no configured recogniser for this language.", unavailable=True)
    return (Path(settings.paddle_det_model_dir or root / "PP-OCRv5_mobile_det"),
            Path(settings.paddle_rec_model_dir or root / name))


def readiness(settings: Settings, language: str = "en") -> dict:
    try:
        detector, recogniser = models(settings, language)
    except RecognitionError as error:
        return {"ready": False, "reason": str(error)}
    if not importlib.util.find_spec("paddle") or not importlib.util.find_spec("paddleocr"):
        return {"ready": False, "reason": "Install PaddlePaddle CPU and PaddleOCR in this environment."}
    if not all((folder / "inference.yml").is_file() for folder in (detector, recogniser)):
        return {"ready": False, "reason": "PaddleOCR detector or recognition weights are not provisioned."}
    return {"ready": True, "reason": None, "model": recogniser.name}


def worker(connection, detector: Path, recogniser: Path) -> None:
    try:
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
        cache = Path(sys.prefix) / "ocr-models" / "paddle-cache"
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache)
        os.environ["PADDLE_HOME"] = str(cache / "paddle")
        os.environ["MPLCONFIGDIR"] = str(cache / "matplotlib")
        cache.mkdir(parents=True, exist_ok=True)
        import cv2
        from paddleocr import PaddleOCR

        ocr = PaddleOCR(device="cpu", use_doc_orientation_classify=False, use_doc_unwarping=False,
                        use_textline_orientation=False, enable_mkldnn=False, cpu_threads=2,
                        text_detection_model_name=detector.name, text_detection_model_dir=str(detector),
                        text_recognition_model_name=recogniser.name, text_recognition_model_dir=str(recogniser))
        connection.send({"ready": True})
        while (image := connection.recv()) is not None:
            result = next(iter(ocr.predict(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR))), {})
            fields = ("rec_texts", "rec_scores", "rec_polys")
            connection.send({"output": [list(result.get(field, [])) for field in fields]})
    except EOFError:
        pass
    except Exception as error:
        connection.send({"error": f"PaddleOCR failed to initialise or process the page ({type(error).__name__})."})
    finally:
        connection.close()


class PaddleOCR:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.process = None
        self.connection = None
        self.weights = None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.process.close()
            self.process = None

    def recognise(self, page, language: str, dpi: int) -> Recognition:
        status = readiness(self.settings, language)
        if not status["ready"]:
            raise RecognitionError(status["reason"], unavailable=True)
        weights = models(self.settings, language)
        deadline = time.monotonic() + max(1, self.settings.paddle_ocr_timeout_s)
        if self.process is None or self.weights != weights:
            self.close()
            context = multiprocessing.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=worker, args=(child, *weights), daemon=True)
            try:
                self.process.start()
            finally:
                child.close()
            self.weights = weights
            self._receive(deadline)
        effective_dpi = min(dpi, max(1, int(1800 * 72 / max(page.rect.width, page.rect.height))))
        self.connection.send(render_gray(page, effective_dpi))
        payload = self._receive(deadline)
        return parse(page.number + 1, payload["output"], effective_dpi, status["model"])

    def _receive(self, deadline: float) -> dict:
        if not self.connection.poll(max(0, deadline - time.monotonic())):
            self.close()
            raise RecognitionError("PaddleOCR timed out.")
        try:
            payload = self.connection.recv()
        except EOFError as error:
            self.close()
            raise RecognitionError("The PaddleOCR worker stopped unexpectedly.") from error
        if "error" in payload:
            self.close()
            raise RecognitionError(payload["error"])
        return payload


def parse(page: int, output: list, dpi: int, model: str) -> Recognition:
    result = Recognition(page, "paddleocr", raw={"model": model, "dpi": dpi, "regions": []})
    texts, scores, polygons = output
    if not (len(texts) == len(scores) == len(polygons)):
        result.issues.append("PaddleOCR returned mismatched text, confidence and region counts.")
        return result
    scale = 72 / dpi
    for text, score, polygon in zip(texts, scores, polygons):
        if not isinstance(text, str) or not text.strip():
            continue
        coordinates = [[float(x), float(y)] for x, y in polygon]
        if len(coordinates) < 3:
            result.issues.append("PaddleOCR returned an invalid region.")
            continue
        x, y = zip(*coordinates)
        box = (min(x) * scale, min(y) * scale, max(x) * scale, max(y) * scale)
        confidence = float(score) * 100
        result.raw["regions"].append({"text": text, "bbox": box, "confidence": confidence})
        result.lines.append(Line(text, box, size=max(1, (box[3] - box[1]) * 0.8),
                                 source="ocr", conf=confidence, rtl=is_rtl(text)))
    if result.lines:
        result.confidence = sum(line.conf for line in result.lines) / len(result.lines)
    return result
