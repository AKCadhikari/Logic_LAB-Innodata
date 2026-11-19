import copy
import json

from comparison import ollama


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
    ollama.annotate(changes, settings, audit)
    assert changes == original
    assert audit.records[-1]["action"] == "assistance_unavailable"


def test_ollama_structured_hint_preserves_deterministic_locations(settings, audit, monkeypatch):
    settings.ollama_enabled = True
    def respond(settings, path, payload=None):
        if path == "/api/tags":
            return {"models": [{"name": settings.ollama_model}]}
        assert payload["options"]["num_gpu"] == 0
        return {"message": {"content": json.dumps({"relation": "uncertain"})}}
    monkeypatch.setattr(ollama, "request_json", respond)
    changes = pair()
    ollama.annotate(changes, settings, audit)
    assert changes[0]["assistance"] == {"relation": "uncertain", "candidate": {"page": 2, "idx": 4}}
    assert changes[0]["change_type"] == "deleted"
    assert changes[1]["change_type"] == "added"
    assert "Personnel" not in str(audit.records)
