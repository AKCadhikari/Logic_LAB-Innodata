"""Transport and model discovery for the local Ollama installation."""
from __future__ import annotations

import json
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


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

    def fetch():
        with opener.open(request, timeout=timeout) as response:
            return response.read(limit + 1)

    try:
        if payload is None:
            raw = fetch()
        else:
            with _inference:
                raw = fetch()
    except HTTPError as error:
        hints = {404: " Check that the configured model is installed.", 500: " Check the Ollama server log."}
        status = "unavailable" if error.code in {404, 503} else "error"
        reason = ""
        if detailed:
            try:
                value = json.loads(error.read(4096))
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
        if len(raw) > limit:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError
    except ValueError as error:
        raise OllamaError("Ollama returned an invalid or oversized response.") from error
    return data


def model_info(model: str, models: list, *, capability: str | None = None) -> dict:
    target = model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"
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


def installed_models(data: dict) -> list:
    models = data.get("models")
    if not isinstance(models, list):
        raise OllamaError("Ollama returned an invalid model list.")
    return models
