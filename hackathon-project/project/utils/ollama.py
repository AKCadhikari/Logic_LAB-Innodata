"""Transport and model discovery for the local Ollama installation."""
from __future__ import annotations

import json
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from utils import execution_audit as audit


class OllamaError(RuntimeError):
    def __init__(self, message: str, status: str = "invalid"):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_inference = Lock()


def local_endpoint(url: str):
    try:
        if not isinstance(url, str):
            raise ValueError
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError as error:
        raise OllamaError("Configure a loopback HTTP endpoint for Ollama.", "unavailable") from error
    if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment or port == 0):
        raise OllamaError("Configure a loopback HTTP endpoint for Ollama.", "unavailable")
    return endpoint


def request_json(url: str, path: str, payload: dict | None, timeout: int, *, detailed: bool = False) -> dict:
    endpoint = local_endpoint(url)
    request = Request(f"http://{endpoint.netloc}{path}",
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    limit = 1024 * 1024
    event = audit.active.get() if payload is not None else None

    def fetch():
        if event is not None:
            event.update(request_status="attempted", endpoint=f"http://{endpoint.netloc}{path}")
        with opener.open(request, timeout=timeout) as response:
            if event is not None:
                event.update(request_status="sent", http_status=response.status)
            return response.read(limit + 1)

    try:
        if payload is None:
            raw = fetch()
        else:
            with _inference:
                raw = fetch()
    except HTTPError as error:
        if event is not None:
            event.update(request_status="sent", http_status=error.code, response_status="http_error")
        hints = {404: " Check that the configured model is installed.", 500: " Check the Ollama server log."}
        status = "unavailable" if error.code in {404, 503} else "error"
        reason = ""
        if detailed or event is not None:
            try:
                body = error.read(4097)
                if event is not None and len(body) <= 4096:
                    event["response_sha256"] = audit.digest(body)
                if detailed:
                    value = json.loads(body)
                    if isinstance(value, dict) and isinstance(value.get("error"), str):
                        reason = " " + " ".join(value["error"].split())[:600]
            except (ValueError, OSError):
                pass
        raise OllamaError(f"Ollama returned HTTP {error.code}.{reason or hints.get(error.code, '')}", status) from error
    except (TimeoutError, URLError, OSError) as error:
        if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
            raise OllamaError("Ollama inference timed out." if payload is not None
                              else "Ollama connection timed out.", "timeout") from error
        raise OllamaError("Ollama is unreachable. Start the local Ollama server.", "unavailable") from error
    try:
        if event is not None:
            event.update(response_status="received", response_sha256=audit.digest(raw))
        if len(raw) > limit:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError
    except ValueError as error:
        if event is not None:
            event["response_status"] = "invalid"
        raise OllamaError("Ollama returned an invalid or oversized response.") from error
    if event is not None:
        message = data.get("message")
        returned_text = data.get("response") or (message.get("content") if isinstance(message, dict) else None)
        audit.response(event, data, raw=raw, executed=bool(
            data.get("model") and (data.get("eval_count", 0) or data.get("prompt_eval_count", 0) or returned_text)))
        event.update(returned_model=data.get("model"), model_done=data.get("done"),
                     performance={key: data[key] for key in audit.METRICS if key in data})
    return data


def model_info(model: str, models: list, *, capability: str | None = None) -> dict:
    target = tagged_model(model)
    match = next((item for item in models if isinstance(item, dict) and item.get("name") == target
                  and not item.get("remote_host") and not item.get("remote_model")
                  and not target.casefold().endswith((":cloud", "-cloud"))), None)
    reason = None
    if not match:
        reason = f"Local Ollama model {model} is not installed."
    elif capability and "capabilities" in match and capability not in (match["capabilities"] or []):
        reason = f"Ollama model {model} does not support {capability}."
    return {"ready": reason is None, "model": model, "digest": match.get("digest") if match else None,
            "reason": reason}


def tagged_model(name: str) -> str:
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def installed_models(data: dict) -> list:
    models = data.get("models")
    if not isinstance(models, list):
        raise OllamaError("Ollama returned an invalid model list.")
    return models
