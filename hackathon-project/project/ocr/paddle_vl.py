"""Image recognition through the explicitly configured local Ollama PaddleOCR-VL tag."""
import base64

from ocr.glm import parse
from ocr.recognition import RecognitionError
from utils import execution_audit as audit, ollama

ENGINE = "paddleocr-vl"


def model_info(settings):
    name, url = settings.paddle_vl_model, settings.paddle_vl_url
    state = {"model": name, "connected": False, "installed": None, "ready": False, "digest": None}
    try:
        tags = ollama.installed_models(ollama.request_json(url, "/api/tags", None, 3))
        inventory = ollama.model_info(name, tags)
        state.update(connected=True, installed=inventory["ready"], **{
            "digest": inventory["digest"], "reason": inventory["reason"]})
        if not inventory["ready"]:
            return state
        info = ollama.request_json(url, "/api/show", {"model": name}, 5)
        capabilities = info.get("capabilities") or []
        vision = "vision" in capabilities
        return {**state, "ready": vision, "capabilities": capabilities, "reason": None if vision else
                f"{name} is installed, but Ollama does not advertise image input (vision projector unavailable)."}
    except ollama.OllamaError as error:
        return {**state, "reason": str(error)}


def recognise(page, dpi, settings):
    import pymupdf

    scale = min(dpi / 72, max(256, settings.glm_ocr_max_edge) / max(page.rect.width, page.rect.height))
    try:
        image = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False).tobytes("png")
    except (RuntimeError, ValueError) as error:
        raise RecognitionError("The source page could not be rendered as a PNG for PaddleOCR-VL.") from error
    name, number = settings.paddle_vl_model, page.number + 1
    payload = {"model": name, "prompt": "OCR:", "images": [base64.b64encode(image).decode("ascii")],
               "stream": False, "keep_alive": settings.paddle_vl_keep_alive,
               "options": {"temperature": 0, "num_gpu": 0, "num_ctx": 8192, "num_predict": settings.glm_ocr_tokens}}
    with audit.execution("page_ocr", "ollama", name, page=number, input_type="rendered_png",
                         input_sha256=audit.digest(image), input_reference=f"pdf/page/{number}/render",
                         prompt_template="paddleocr-vl/ocr/v1") as event:
        try:
            data = ollama.request_json(settings.paddle_vl_url, "/api/generate", payload,
                                       max(1, settings.paddle_vl_timeout_s), detailed=True)
        except ollama.OllamaError as error:
            raise RecognitionError(str(error), status=error.status) from error
        result = parse(number, data, engine=ENGINE)
        returned = data.get("model")
        if not isinstance(returned, str) or ollama.tagged_model(returned).casefold() != ollama.tagged_model(name).casefold():
            result.issues.append("Ollama did not confirm the requested PaddleOCR-VL model identity.")
        if event:
            result.execution_ids.append(event["execution_id"])
        return result
