import json
import os
import socket
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import startup
from config.settings import Settings
from utils.ollama import OllamaError


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def fake_api(monkeypatch):
    calls, resident = [], {}
    settings = Settings(ocr_automatic_secondary="paddleocr")
    tags = [{"name": model["name"], "size": 1, "digest": model["role"]}
            for model in startup.configured_models(settings)]
    failures = {}

    def request(url, path, payload=None, timeout=2):
        calls.append((path, payload, timeout))
        if path == "/api/version":
            return {"version": "0.40.1"}
        if path == "/api/tags":
            return {"models": tags}
        if path == "/api/ps":
            return {"models": [{"name": name, "size_vram": 0} for name in resident]}
        name = payload["model"]
        if name in failures:
            raise failures[name]
        if payload["keep_alive"] == "0":
            resident.pop(name, None)
        else:
            resident[name] = True
        return {"done": True, "response": "42"}

    monkeypatch.setattr(startup, "ollama_json", request)
    monkeypatch.setattr(startup, "find_ollama", lambda settings: "local-ollama")
    monkeypatch.setattr(startup.psutil, "virtual_memory", lambda: SimpleNamespace(available=20 * 2**30))
    return settings, tags, resident, calls, failures


def test_both_installed_and_loaded_without_starting_another_server(tmp_path, fake_api, monkeypatch):
    settings, _, resident, calls, _ = fake_api
    manager = startup.StartupManager(settings, tmp_path / "workspace", tmp_path / "logs", 8765)
    monkeypatch.setattr(manager, "spawn", lambda *args: pytest.fail("Ollama is already serving"))
    manager.prepare()
    assert manager.report["services"][0]["source"] == "reused"
    assert manager.report["status"] == "ready"
    assert len(resident) == 2
    assert all(row["installed"] and row["loaded"] and row["warmup"] == "passed" for row in manager.report["models"])
    generations = [payload for path, payload, _ in calls if path == "/api/generate"]
    assert generations[1]["images"] and generations[-1]["prompt"]
    audit = [json.loads(line) for line in (manager.logs / "startup.log").read_text().splitlines()]
    assert sum(row["stage"] == "startup_warmup" and row["synthetic"] for row in audit) == 2


@pytest.mark.parametrize("missing", ["ocr", "review"])
def test_missing_model_is_reported_and_never_downloaded(tmp_path, fake_api, missing):
    settings, tags, _, calls, _ = fake_api
    missing_name = next(row["name"] for row in startup.configured_models(settings) if row["role"] == missing)
    tags[:] = [row for row in tags if row["name"] != missing_name]
    manager = startup.StartupManager(settings, tmp_path, tmp_path, 8765)
    manager.prepare()
    row = next(row for row in manager.report["models"] if row["role"] == missing)
    assert manager.report["status"] == "degraded"
    assert row["installed"] is False and row["installation"] == f"ollama pull {missing_name}"
    assert not any(path == "/api/pull" or payload and payload["model"] == missing_name for path, payload, _ in calls)


@pytest.mark.parametrize("failure", ["timeout", "resource"])
def test_inference_failure_keeps_other_model_available(tmp_path, fake_api, failure):
    settings, _, _, calls, failures = fake_api
    settings.ollama_model_timeout = 7
    detail = {"timeout": "Ollama inference timed out.", "resource": "Model requires more system memory."}[failure]
    failures[settings.ollama_model] = OllamaError(detail, failure)
    manager = startup.StartupManager(settings, tmp_path, tmp_path, 8765)
    manager.prepare()
    ocr, qwen = manager.report["models"]
    assert manager.report["status"] == "degraded"
    assert ocr["warmup"] == "passed" and qwen["error"] == detail
    assert all(timeout == 7 for path, _, timeout in calls if path == "/api/generate")


@pytest.mark.parametrize("mode", ["ocr-only", "sequential"])
def test_resource_modes(tmp_path, fake_api, mode):
    settings, _, resident, _, _ = fake_api
    settings.ollama_startup_mode = mode
    manager = startup.StartupManager(settings, tmp_path, tmp_path, 8765)
    manager.prepare()
    ocr, qwen = manager.report["models"]
    if mode == "ocr-only":
        assert qwen["warmup"] == "on_demand" and list(resident) == [settings.glm_ocr_model]
    else:
        assert ocr["released_by_policy"] and ocr["warmup"] == "passed" and list(resident) == [settings.ollama_model]


def test_memory_guard_and_eviction(tmp_path, fake_api, monkeypatch):
    settings, _, resident, _, _ = fake_api
    manager = startup.StartupManager(settings, tmp_path, tmp_path, 8765)
    monkeypatch.setattr(startup.psutil, "virtual_memory", lambda: SimpleNamespace(available=1))
    manager.prepare()
    assert all("Memory preflight" in model["error"] for model in manager.report["models"])
    assert not resident


def test_configured_env_file_is_shared_by_app_and_launcher(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OLLAMA_STARTUP_MODE=sequential\nGLM_OCR_MODEL=example-vision:v2\n", encoding="utf-8")
    settings = Settings.load(env_file=env)
    assert settings.ollama_startup_mode == "sequential"
    assert startup.configured_models(settings)[0]["name"] == "example-vision:v2"


@pytest.mark.parametrize("kind", ["foreign-ollama", "missing-executable"])
def test_service_will_not_replace_port_or_launch_missing_executable(tmp_path, monkeypatch, kind):
    occupied = kind == "foreign-ollama"
    def foreign(*args, **kwargs):
        raise OllamaError("Foreign service")
    monkeypatch.setattr(startup, "ollama_json", foreign)
    monkeypatch.setattr(startup, "port_open", lambda *_: occupied)
    manager = startup.StartupManager(Settings(ollama_startup_timeout=1), tmp_path, tmp_path, 8765)
    monkeypatch.setattr(manager, "spawn", lambda *args: pytest.fail("Should not spawn"))
    with pytest.raises(startup.StartupError, match="occupied" if occupied else "executable was not found"):
        manager.service(manager.settings.ollama_url, None)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher integration")
@pytest.mark.parametrize("entry", ["workspace", "starter"])
def test_windows_click_twice_shutdown_and_restart(tmp_path, entry):
    port = free_port()
    storage, logs = tmp_path / "workspace", tmp_path / "logs"
    batch = ["cmd.exe", "/d", "/c", "Start_DocuSync_AI.bat"]
    ps = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "start_doc_sync.ps1"]
    launcher_dir = (startup.ROOT if entry == "workspace" else
                    startup.ROOT.parent / "DocuSyncAI-starter" / "DocuSyncAI")
    args = ["-NoBrowser", "-Port", str(port), "-Storage", str(storage), "-Logs", str(logs)]
    env = {**os.environ, "OLLAMA_STARTUP_ENABLED": "false"}

    def launch(command, extra=()):
        result = subprocess.run([*command, *args, *extra], cwd=launcher_dir, env=env,
                                capture_output=True, text=True, timeout=35)
        assert result.returncode == 0, result.stdout + result.stderr

    with startup.startup_lock(logs):
        launch(batch)
        assert not startup.port_open("127.0.0.1", port)
    try:
        launch(batch)
        before = startup.read_json(logs / "owned-processes.json")["backend"]
        launch(batch)
        assert startup.read_json(logs / "owned-processes.json")["backend"] == before
        launch(ps if entry == "workspace" else batch, ("-Action", "Restart"))
        after = startup.read_json(logs / "owned-processes.json")["backend"]
        assert after != before
        assert startup.read_json(logs / "startup-status.json")["frontend_ready"]
    finally:
        if startup.port_open("127.0.0.1", port):
            launch(ps if entry == "workspace" else batch, ("-Action", "Stop"))
    assert not startup.port_open("127.0.0.1", port)


@pytest.mark.parametrize("component", ["backend", "frontend", "out of date"])
def test_distinguishes_backend_and_frontend_failure(tmp_path, component):
    settings = Settings.load()
    storage = tmp_path / "workspace"
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/startup/identity" or component == "backend":
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            identity = {"workspace": startup.workspace_id(storage), "backend_ready": True,
                        "settings": startup.settings_id(settings)}
            if component != "out of date":
                identity["workspace_api_version"] = startup.WORKSPACE_API_VERSION
            self.wfile.write(json.dumps(identity).encode())
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        manager = startup.StartupManager(settings, storage, tmp_path / "logs", server.server_port)
        try:
            with pytest.raises(startup.StartupError, match=component):
                manager.ensure_backend(None)
            assert bool(manager.report.get("backend_ready")) == (component == "frontend")
        finally:
            server.shutdown()
            worker.join()
