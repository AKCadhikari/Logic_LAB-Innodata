import copy
import json

import pytest

from comparison import ollama
from config.settings import Settings


def pair():
    return [
        {"category": "text", "change_type": "deleted", "old_content": "Personnel costs this quarter",
         "original_location": {"page": 1, "idx": 0}},
        {"category": "text", "change_type": "added", "new_content": "Equipment costs this year",
         "new_location": {"page": 2, "idx": 4}},
    ]


def test_ollama_unavailable_keeps_comparison(settings, audit, monkeypatch):
    settings.ollama_enabled = True
    def unavailable(*args):
        raise OSError("Ollama is unavailable.")
    monkeypatch.setattr(ollama, "request_json", unavailable)
    changes = pair()
    original = copy.deepcopy(changes)
    status = ollama.annotate(changes, settings, audit)
    assert changes == original
    assert status["status"] == "unavailable"
    assert audit.records[-1]["action"] == "assistance_unavailable"


def test_ollama_structured_hint_preserves_deterministic_locations(settings, audit, monkeypatch):
    settings.ollama_enabled = True
    def respond(settings, path, payload=None):
        if path == "/api/tags":
            return {"models": [{"name": settings.ollama_model}]}
        assert path == "/api/chat" and payload["model"] == settings.ollama_model
        assert payload["format"]["required"] == ["change_1"]
        assert list(json.loads(payload["messages"][-1]["content"])) == ["change_1"]
        assert payload["options"]["num_gpu"] == payload["options"]["temperature"] == 0
        assert payload["keep_alive"] == settings.ollama_keep_alive and not payload["think"]
        return {"done": True, "message": {"content": json.dumps({"change_1":
            {"relation": "uncertain", "classification": "terminology", "review_focus": "term_meaning"},
        })}}
    monkeypatch.setattr(ollama, "request_json", respond)
    changes = pair()
    status = ollama.annotate(changes, settings, audit)
    assert status["status"] == "completed" and status["reviewed"] == 1
    assert changes[0]["assistance"]["relation"] == "uncertain"
    assert changes[0]["assistance"]["candidate"] == {"page": 2, "idx": 4}
    assert changes[0]["change_type"] == "deleted"
    assert changes[1]["change_type"] == "added"
    assert "Personnel" not in str(audit.records)


@pytest.mark.parametrize(("key", "extra"), [
    ("change_3", {}),
    ("change_1", {"replacement": "invented correction"}),
])
def test_invalid_qwen_assessment_keeps_numeric_evidence(settings, audit, monkeypatch, key, extra):
    def respond(settings, path, payload=None):
        if path == "/api/tags":
            return {"models": [{"name": settings.ollama_model}]}
        return {"done": True, "message": {"content": json.dumps({key: {
            "relation": "related", "classification": "numerical", "review_focus": "values_and_units", **extra,
        }})}}
    monkeypatch.setattr(ollama, "request_json", respond)
    change = {"category": "table", "change_type": "modified", "can_sync": False,
              "old_content": "\n0,0 (1×1): 240%", "new_content": "\n0,1 (1×1): 260%"}
    assert ollama.annotate([change], settings, audit)["status"] == "failed"
    assert change["numerical_change"] == {"before": ["240%"], "after": ["260%"]}
    assert "assistance" not in change and not change["can_sync"]


def test_ollama_environment_configuration(monkeypatch):
    for key, value in {"OLLAMA_BASE_URL": "http://127.0.0.1:12345", "OLLAMA_TIMEOUT": "23",
                       "GLM_OCR_MODEL": "glm-ocr:latest", "QWEN_MODEL": "qwen3:test",
                       "OLLAMA_KEEP_ALIVE": "30s"}.items():
        monkeypatch.setenv(key, value)
    settings = Settings.load()
    assert settings.glm_ocr_url == settings.ollama_url == "http://127.0.0.1:12345"
    assert settings.glm_ocr_timeout_s == settings.ollama_timeout_s == 23
    assert settings.glm_ocr_keep_alive == settings.ollama_keep_alive == "30s"
    assert settings.glm_ocr_model == "glm-ocr:latest" and settings.ollama_model == "qwen3:test"
