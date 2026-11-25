"""Local inference evidence. Public records contain hashes and structured metadata."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from uuid import uuid4

_trace = ContextVar("execution_trace", default=None)
_details = ContextVar("execution_details", default={})
active = ContextVar("active_execution", default=None)
test_mode = ContextVar("execution_mode", default="DOCUMENT_PROCESSING")
METRICS = ("total_duration", "load_duration", "prompt_eval_count", "prompt_eval_duration",
           "eval_count", "eval_duration", "prompt_eval_cached_count")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def digest(value):
    return sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def current():
    return _trace.get()


@contextmanager
def scope(document_id, source_hash, private=None, trace_id=None):
    state = {"trace_id": trace_id or uuid4().hex, "document_id": document_id,
             "document_sha256": source_hash, "mode": test_mode.get(), "executions": [], "private": private}
    token = _trace.set(state)
    try:
        yield state
    finally:
        _trace.reset(token)


@contextmanager
def details(**values):
    token = _details.set({**_details.get(), **values})
    try:
        yield
    finally:
        _details.reset(token)


@contextmanager
def execution(task, provider, model, **fields):
    state = current()
    if state is None:
        yield {}
        return
    item = {key: state[key] for key in ("trace_id", "document_id", "document_sha256", "mode")}
    item.update(execution_id=uuid4().hex, started_at=timestamp(), task=task, provider=provider,
                model=model, request_status="not_sent", response_status="not_received", executed=False,
                validation="not_checked", downstream=[])
    item.update(_details.get())
    item.update(fields)
    state["executions"].append(item)
    start = monotonic()
    token = active.set(item)
    try:
        yield item
    except Exception as error:
        item["error"] = {"type": type(error).__name__, "status": getattr(error, "status", "error")}
        if type(error).__name__ in {"OllamaError", "RecognitionError"}:
            item["error"]["message"] = str(error)
        if item["request_status"] in {"sent", "attempted"} and item["response_status"] == "not_received":
            item["response_status"] = "failed"
        raise
    finally:
        item.update(completed_at=timestamp(), duration_ms=round((monotonic() - start) * 1000, 3))
        active.reset(token)


def response(item, payload, *, executed, raw=None):
    if not item:
        return
    data = raw if raw is not None else canonical(payload)
    item.update(response_status="received", response_sha256=digest(data), executed=executed)
    state = current()
    if state and state["private"]:
        name = item["execution_id"] + ".json"
        folder = Path(state["private"])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / name).open("xb") as file:
                file.write(data)
            item["private_response_reference"] = name
        except OSError:
            item["private_evidence_error"] = "Could not retain optional response evidence."


def mark(ids, **values):
    if state := current():
        for item in state["executions"]:
            if item["execution_id"] in ids:
                item.update(values)


def since(start):
    state = current()
    return state["executions"][start:] if state else []


def cursor():
    return len(state["executions"]) if (state := current()) else 0


def ids_since(start):
    return [item["execution_id"] for item in since(start)]
