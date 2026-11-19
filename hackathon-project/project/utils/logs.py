"""Structured audit log (JSON lines) + console logger. Every processing decision goes through here."""
from __future__ import annotations
import json, logging, sys, time, threading
from pathlib import Path

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, stage: str, action: str, **data):
        rec = {"t": round(time.time(), 3), "stage": stage, "action": action, **data}
        with _lock:
            self.records.append(rec)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec


def get_logger(name="docusync"):
    lg = logging.getLogger(name)
    if not lg.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg
