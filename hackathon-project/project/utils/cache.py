"""Tiny page-level JSON cache (OCR results are the expensive part)."""
from __future__ import annotations
import hashlib, json
from pathlib import Path


def file_key(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()[:16]


class Cache:
    def __init__(self, root: str | Path, enabled: bool = True):
        self.root, self.enabled = Path(root), enabled

    def _p(self, *parts) -> Path:
        return self.root.joinpath(*[str(x).replace("/", "_").replace("+", "-") for x in parts[:-1]], str(parts[-1]) + ".json")

    def get(self, *parts):
        if not self.enabled:
            return None
        p = self._p(*parts)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except Exception:
            return None

    def put(self, value, *parts):
        if not self.enabled:
            return
        try:
            p = self._p(*parts); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
