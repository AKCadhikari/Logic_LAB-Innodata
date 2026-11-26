"""Prepare local Ollama models and manage the existing Windows workspace server."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

import psutil

from config.settings import Settings
from utils import ollama
from utils.logs import AuditLog

ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*:[A-Za-z0-9][A-Za-z0-9_.-]*")


class StartupError(RuntimeError):
    pass


class StartupBusy(StartupError):
    pass


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


@contextmanager
def startup_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "startup.lock").open("a+b") as handle:
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise StartupBusy("A DocuSync startup, retry, or shutdown is already in progress.") from error
        yield


def workspace_id(storage: Path) -> str:
    return sha256(f"{ROOT}\n{storage.resolve()}".casefold().encode()).hexdigest()


def settings_id(settings: Settings) -> str:
    return sha256(json.dumps(settings.to_dict(), sort_keys=True).encode()).hexdigest()


def process_record(process):
    info = psutil.Process(process.pid)
    return {"pid": info.pid, "created": info.create_time()}


def owned_process(record):
    try:
        process = psutil.Process(record["pid"])
        if abs(process.create_time() - record["created"]) <= 0.001 and process.is_running():
            return process
    except (KeyError, psutil.Error):
        pass
    return None


def process_owns_pid(process, pid):
    try:
        return process.pid == pid or process in psutil.Process(pid).parents()
    except psutil.Error:
        return False


def stop_owned_tree(process):
    children = process.children(recursive=True)
    process.terminate()
    process.wait(timeout=10)
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(children, timeout=5)
    if remaining:
        raise StartupError("Some launcher-owned Ollama processes are still shutting down.")


def localhost_http(url, *, key=None):
    request = Request(url, data=b"" if key else None, headers={"X-DocuSync-Launcher-Key": key} if key else {})
    opener = build_opener(ProxyHandler({}), ollama.NoRedirect())
    with opener.open(request, timeout=2) as response:
        return response.read(1024 * 1024)


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=.35):
            return True
    except OSError:
        return False


def find_ollama(settings):
    name = "ollama.exe" if sys.platform == "win32" else "ollama"
    if settings.ollama_executable:
        candidate = Path(settings.ollama_executable).expanduser()
        candidate = candidate if candidate.is_absolute() else ROOT / candidate
        return str(candidate) if candidate.is_file() else None
    if executable := shutil.which(name):
        return executable
    if sys.platform == "win32":
        for prefix, suffix in (("LOCALAPPDATA", "Programs/Ollama"), ("ProgramFiles", "Ollama")):
            if os.getenv(prefix) and (candidate := Path(os.environ[prefix], suffix, name)).is_file():
                return str(candidate)
    return None


def ollama_json(url, endpoint, payload=None, timeout=2):
    return ollama.request_json(url, endpoint, payload, timeout, detailed=True)


def probe_image():
    from PIL import Image, ImageDraw

    image = Image.new("L", (192, 100), color=255)
    ImageDraw.Draw(image).text((39, 17), "42", fill=0, font_size=68)
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")


def configured_models(settings):
    models = []
    enabled = {settings.ocr_backend}
    if settings.ocr_backend == "automatic":
        enabled = {settings.ocr_automatic_primary, settings.ocr_automatic_secondary, settings.ocr_automatic_tertiary}
    if settings.ocr_enabled and "glm-ocr" in enabled:
        models.append({"role": "ocr", "label": "GLM-OCR", "name": settings.glm_ocr_model,
                       "url": settings.glm_ocr_url, "capability": "vision"})
    if settings.ocr_enabled and "paddleocr-vl" in enabled:
        models.append({"role": "ocr", "label": "PaddleOCR-VL", "name": settings.paddle_vl_model,
                       "url": settings.paddle_vl_url, "capability": "vision", "is_paddle_vl": True,
                       "on_demand": settings.ocr_backend == "automatic" and settings.ocr_automatic_primary != "paddleocr-vl"})
    if settings.ollama_enabled:
        models.append({"role": "review", "label": "Qwen", "name": settings.ollama_model,
                       "url": settings.ollama_url, "capability": "completion"})
    return models


def loaded_models(url):
    return {entry["name"]: {key: entry[key] for key in ("size", "size_vram", "context_length", "expires_at", "runner")
                            if key in entry} for entry in ollama.installed_models(ollama_json(url, "/api/ps"))
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)}


class StartupManager:
    def __init__(self, settings, storage, logs, port):
        self.settings, self.storage, self.logs, self.port = settings, storage, logs, port
        self.base = f"http://127.0.0.1:{port}"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(logs / "startup.log")
        self.report_path = logs / "startup-status.json"
        self.process_path = logs / "owned-processes.json"
        self.report = read_json(self.report_path)
        self.processes = read_json(self.process_path)

    def event(self, stage, action, **details):
        self.audit.event(stage, action, timestamp=datetime.now(timezone.utc).isoformat(), **details)

    def publish(self, **changes):
        self.report.update(changes, updated=datetime.now(timezone.utc).isoformat())
        save_json(self.report_path, self.report)

    def remember(self, slot, child, **extra):
        self.processes[slot] = {**process_record(child), **extra}
        save_json(self.process_path, self.processes)

    def spawn(self, args, filename, env=None):
        with (self.logs / filename).open("ab") as log:
            return subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def wait_for(self, check, timeout, child=None):
        end = time.monotonic() + timeout
        while True:
            try:
                return check()
            except (OSError, ValueError, StartupError, ollama.OllamaError) as error:
                if (child is not None and child.poll() is not None) or time.monotonic() >= end:
                    raise StartupError(str(error)) from error
            time.sleep(.35)

    def service(self, url, exe):
        endpoint = ollama.local_endpoint(url)
        host = "127.0.0.1" if endpoint.hostname == "localhost" else endpoint.hostname
        port = endpoint.port or 80
        bind = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

        def health():
            version = ollama_json(url, "/api/version").get("version")
            if not isinstance(version, str) or not re.match(r"^\d+\.\d+", version):
                raise StartupError("This listener did not identify itself as Ollama.")
            return version, ollama.installed_models(ollama_json(url, "/api/tags"))

        key = "ollama:" + bind
        self.event("startup_service", "api_check", url=url)
        try:
            version, tags = health()
            source = "launcher-owned" if owned_process(self.processes.get(key, {})) else "reused"
        except (ollama.OllamaError, StartupError):
            if port_open(endpoint.hostname, port):
                try:
                    version, tags = self.wait_for(health, min(5, self.settings.ollama_startup_timeout))
                except StartupError as error:
                    raise StartupError(f"Port {port} is occupied and its Ollama API is unhealthy. {error}") from error
                source = "launcher-owned" if owned_process(self.processes.get(key, {})) else "reused"
            else:
                if not exe:
                    raise StartupError("Ollama executable was not found. Install Ollama for Windows or set OLLAMA_EXECUTABLE.")
                self.event("startup_service", "launch_requested", url=url, executable=exe)
                child = self.spawn([exe, "serve"], f"ollama-{port}.log", {**os.environ, "OLLAMA_HOST": bind})
                try:
                    self.remember(key, child)
                    version, tags = self.wait_for(health, self.settings.ollama_startup_timeout, child)
                except (OSError, psutil.Error, StartupError) as error:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)
                    if child.returncode:
                        # The Windows Ollama tray app may have started the service at the same time.
                        try:
                            version, tags = health()
                        except (ollama.OllamaError, StartupError):
                            raise StartupError(f"Ollama API startup failed. {error} See logs/ollama-{port}.log.") from error
                        source = "reused"
                    else:
                        raise
                else:
                    source = "started"
        self.event("startup_service", "api_healthy", url=url, version=version, source=source)
        return {"url": url, "running": True, "healthy": True, "version": version, "source": source}, tags

    def warm(self, model, *, release, tag):
        s = self.settings
        url, name = model["url"], model["name"]
        already = name in loaded_models(url)
        available = psutil.virtual_memory().available
        model["available_system_memory_bytes"] = available
        required = int(tag.get("size") or 0) + s.ollama_startup_reserve_mb * 1024 * 1024
        if not already and available < required:
            raise StartupError(f"Memory preflight deferred {name}. {available // 2**20} MB available, "
                               f"{required // 2**20} MB estimated with reserve. Try sequential or OCR-only mode.")
        options = {"temperature": 0, "num_gpu": 0, "num_predict": 16,
                   "num_ctx": max(8192, s.glm_ocr_tokens + 4096) if model["role"] == "ocr" else 4096}
        payload = {"model": name, "stream": False, "keep_alive": s.ollama_startup_keep_alive, "options": options}
        self.event("startup_model", "load_requested", model=name, url=url, already_loaded=already)
        if ollama_json(url, "/api/generate", payload, s.ollama_model_timeout).get("done") is not True:
            raise StartupError(f"Ollama did not finish loading {name}.")
        resident = loaded_models(url)
        self.event("startup_model", "resident_models_checked", url=url, loaded_models=resident)
        model["loaded_during_startup"] = name in resident
        if not model["loaded_during_startup"]:
            raise StartupError(f"{name} did not appear in Ollama's running model list after loading.")
        model["warmup"] = "failed"
        if release and not already:
            payload["keep_alive"] = "0"
        if model["role"] == "ocr":
            payload.update(prompt="OCR:" if model.get("is_paddle_vl") else "Text Recognition:", images=[probe_image()])
        else:
            payload.update(prompt="Reply with only the digits 42.", think=False)
        response = ollama_json(url, "/api/generate", payload, s.ollama_model_timeout)
        if response.get("done") is not True or not re.search(r"(?<!\d)42(?!\d)", str(response.get("response", ""))):
            raise StartupError(f"{name} did not return the expected text on the synthetic inference check.")
        model["warmup"] = "passed"
        model["released_by_policy"] = release and not already
        self.event("startup_warmup", "inference_passed", model=name, synthetic=True,
                   elapsed_ns=response.get("total_duration"))

    def validate(self):
        s = self.settings
        if not isinstance(s.ollama_startup_mode, str) or s.ollama_startup_mode not in {"all", "ocr-only", "sequential"}:
            raise StartupError("OLLAMA_STARTUP_MODE must be all, ocr-only, or sequential.")
        limits = (s.ollama_startup_timeout, s.ollama_model_timeout, s.ollama_startup_reserve_mb)
        if any(type(limit) is not int for limit in limits) or min(limits[:2]) < 1 or limits[2] < 0:
            raise StartupError("Ollama timeouts must be positive and the memory reserve must be nonnegative.")
        if not isinstance(s.ollama_startup_keep_alive, str) or not re.fullmatch(
                r"(?:[1-9]\d*(?:ms|s|m|h)?|-1)", s.ollama_startup_keep_alive):
            raise StartupError("The startup keep-alive must be positive (for example 30m) or -1.")
        for model in configured_models(s):
            if (not isinstance(model["name"], str) or not MODEL_NAME.fullmatch(model["name"])
                    or model["name"].casefold().endswith((":cloud", "-cloud"))):
                raise StartupError(f"{model['label']} must have an exact local model name and tag.")
            ollama.local_endpoint(model["url"])

    def prepare(self):
        self.validate()
        s = self.settings
        models = configured_models(s)
        from ocr import paddle, paddle_vl
        python_ocr = paddle.readiness(s, s.paddle_ocr_lang or s.paddle_ocr_unknown_lang or "en")
        self.event("startup", "python_ocr_checked", installed=python_ocr["installed"],
                   model=python_ocr.get("model"), weights_available=python_ocr["model_available"])
        exe = find_ollama(s)
        self.event("startup", "prepare", mode=s.ollama_startup_mode, executable=exe,
                   required_models=[model["name"] for model in models])
        for model in models:
            model.update(installed=None, loaded_during_startup=False, loaded=False, warmup="not_run")
        services, tags = {}, {}
        self.publish(status="warming" if s.ollama_startup_enabled else "startup_disabled",
                     mode=s.ollama_startup_mode, launcher_managed=True, ollama_installed=bool(exe),
                     services=[], models=models, python_ocr=python_ocr, error=None)
        if not s.ollama_startup_enabled or not models:
            self.publish(status="startup_disabled" if not s.ollama_startup_enabled else "no_models_required")
            return
        for url in dict.fromkeys(model["url"] for model in models):
            try:
                services[url], inventory = self.service(url, exe)
                tags[url] = {item["name"]: item for item in inventory
                             if isinstance(item, dict) and isinstance(item.get("name"), str)}
            except (OSError, RuntimeError, ValueError) as error:
                endpoint = ollama.local_endpoint(url)
                services[url] = {"url": url, "running": port_open(endpoint.hostname, endpoint.port or 80),
                                 "healthy": False, "error": str(error)}
                self.event("startup_service", "failed", url=url, error=str(error))
        self.publish(services=list(services.values()))
        for model in models:
            inventory = tags.get(model["url"])
            if inventory is not None:
                info = ollama.model_info(model["name"], list(inventory.values()), capability=model["capability"])
                if info["ready"] and model.get("is_paddle_vl"):
                    info = paddle_vl.model_info(s)
                model["installed"] = info["ready"] or bool(info["digest"])
                if not info["ready"]:
                    model["error"] = info["reason"]
                    if not model["installed"]:
                        model["installation"] = f"ollama pull {model['name']}"
            else:
                model["error"] = services[model["url"]]["error"]
            self.event("startup_model", "installation_checked", model=model["name"],
                       installed=model["installed"], error=model.get("error"))
        self.publish(models=models)
        selected = [model for model in models if "error" not in model and not model.get("on_demand")
                    and not (s.ollama_startup_mode == "ocr-only" and model["role"] != "ocr")]
        for model in models:
            if "error" in model:
                continue
            if model not in selected:
                model["warmup"] = "on_demand"
                continue
            try:
                self.warm(model, release=s.ollama_startup_mode == "sequential" and model is not selected[-1],
                          tag=tags[model["url"]][model["name"]])
            except (OSError, ValueError, ollama.OllamaError, StartupError) as error:
                model["warmup"] = "failed"
                model["error"] = str(error)
                self.event("startup_warmup", "failed", model=model["name"], error=str(error))
            self.publish(models=models)
        self.refresh_residency(models, services)
        for model in selected:
            if not model["loaded"] and not model.get("released_by_policy") and "error" not in model:
                model["error"] = f"{model['name']} is no longer resident. Ollama may have evicted it under resource pressure."
        self.publish(models=models, status="degraded" if any(model.get("error") for model in models) else "ready")

    def refresh_residency(self, models, services=None, *, live=False):
        for url in dict.fromkeys(model["url"] for model in models):
            if not live and services is not None and not services[url]["healthy"]:
                continue
            try:
                resident = loaded_models(url)
            except ollama.OllamaError as error:
                if live and services is not None and url in services:
                    endpoint = ollama.local_endpoint(url)
                    services[url].update(running=port_open(endpoint.hostname, endpoint.port or 80),
                                         healthy=False, error=str(error))
                for model in models:
                    if model["url"] == url:
                        model.update(loaded=None, runtime_error=str(error))
                continue
            if live and services is not None and url in services:
                services[url].update(running=True, healthy=True)
                services[url].pop("error", None)
            elif services is not None:
                self.event("startup_model", "final_residency", url=url, loaded_models=resident)
            for model in models:
                if model["url"] == url:
                    model.pop("runtime_error", None)
                    model.update(loaded=model["name"] in resident, residency=resident.get(model["name"], {}))

    def backend_health(self):
        data = json.loads(localhost_http(self.base + "/api/startup/identity"))
        if data.get("workspace") != workspace_id(self.storage) or data.get("backend_ready") is not True:
            raise StartupError(f"Port {self.port} is occupied by a different workspace or service.")
        return data

    def frontend_health(self):
        if b'id="startup-status"' not in localhost_http(self.base + "/"):
            raise StartupError("The DocuSync frontend HTML failed its readiness check.")
        if b"loadStartupStatus" not in localhost_http(self.base + "/static/workspace.js"):
            raise StartupError("The DocuSync frontend script failed its readiness check.")
        if not localhost_http(self.base + "/static/workspace.css"):
            raise StartupError("The DocuSync frontend stylesheet is empty.")

    def ensure_backend(self, config):
        existing = port_open("127.0.0.1", self.port)
        child = None
        try:
            if existing:
                try:
                    identity = self.wait_for(self.backend_health, 3)
                except StartupError as error:
                    raise StartupError(f"Port {self.port} is occupied and the DocuSync backend could not be verified. {error}") from error
                if identity.get("settings") != settings_id(self.settings):
                    raise StartupError("The running workspace uses different settings. Restart it with the launcher.")
                source = "reused"
            else:
                key = secrets.token_hex(32)
                args = [sys.executable, "-B", str(ROOT / "project/ui/server.py"), "--storage", str(self.storage),
                        "--port", str(self.port), "--startup-logs", str(self.logs)]
                if config:
                    args.extend(["--config", str(config)])
                child = self.spawn(args, "backend.log", {**os.environ, "DOCUSYNC_LAUNCHER_KEY": key})
                self.remember("backend", child, key=key, port=self.port, workspace=workspace_id(self.storage))
                self.wait_for(self.backend_health, min(45, self.settings.ollama_startup_timeout), child)
                source = "started"
            self.publish(backend_ready=True)
            self.event("startup_app", "backend_ready", source=source, url=self.base)
            try:
                self.frontend_health()
            except OSError as error:
                raise StartupError(f"DocuSync frontend startup failed. {error}") from error
            self.publish(frontend_ready=True)
            self.event("startup_app", "frontend_ready", url=self.base)
        except (OSError, ValueError, RuntimeError):
            if child and child.poll() is None:
                try:
                    stop_owned_tree(psutil.Process(child.pid))
                except psutil.NoSuchProcess:
                    pass
                self.publish(backend_ready=False)
            raise

    def stop(self):
        record = self.processes.get("backend", {})
        process = owned_process(record)
        if process:
            if record.get("port") != self.port or not process_owns_pid(process, self.backend_health()["pid"]):
                raise StartupError("Backend ownership could not be confirmed.")
            try:
                localhost_http(self.base + "/api/startup/shutdown", key=record["key"])
            except URLError as error:
                raise StartupError("Backend shutdown was refused or unavailable. Finish running jobs and try again.") from error
            process.wait(timeout=20)
            self.event("startup_app", "backend_stopped", pid=process.pid)
        elif port_open("127.0.0.1", self.port):
            raise StartupError("The active backend was started outside this launcher. Close it from its original session.")
        for key, record in self.processes.items():
            if key.startswith("ollama:") and (process := owned_process(record)):
                stop_owned_tree(process)
                self.event("startup_service", "owned_server_stopped", pid=process.pid, service=key)
        self.processes = {}
        save_json(self.process_path, self.processes)
        self.publish(status="stopped", backend_ready=False, frontend_ready=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart"), default="start", nargs="?")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--storage", type=Path, default=ROOT / "storage")
    parser.add_argument("--logs", type=Path, default=ROOT / "logs")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.config:
        args.config = args.config.resolve()
    try:
        with startup_lock(args.logs):
            manager = StartupManager(Settings(), args.storage.resolve(), args.logs.resolve(), args.port)
            try:
                if args.action != "stop":
                    manager.settings = Settings.load(args.config, default_schema_profile="sample")
                    manager.validate()
                if args.action in {"stop", "restart"}:
                    manager.stop()
                if args.action == "stop":
                    print("DocuSync shut down.")
                    return 0
                print("Checking Ollama and warming local models. The first cold start may take several minutes.", flush=True)
                manager.publish(backend_ready=False, frontend_ready=False)
                manager.prepare()
                for model in manager.report["models"]:
                    print(f"{model['label']} ({model['name']}) - {model['warmup']}"
                          + (f". {model['error']}" if model.get("error") else "")
                          + (f" Install with: {model['installation']}" if model.get("installation") else ""))
                manager.ensure_backend(args.config)
                print(f"DocuSync {manager.report['status']} at {manager.base}", flush=True)
                if not args.no_browser:
                    try:
                        opened = webbrowser.open(manager.base)
                    except (OSError, webbrowser.Error):
                        opened = False
                    manager.event("startup_app", "browser_requested" if opened else "browser_failed", url=manager.base)
                    if not opened:
                        print(f"Open {manager.base} in your browser.")
                return 0
            except (OSError, RuntimeError, ValueError, psutil.Error) as error:
                manager.event("startup", "failed", action_requested=args.action, error=str(error))
                manager.publish(status="failed", error=str(error))
                raise
    except StartupBusy as error:
        print(str(error))
        return 0
    except (OSError, RuntimeError, ValueError, psutil.Error) as error:
        print(f"DocuSync startup error: {error}\nDetails: {args.logs / 'startup.log'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
