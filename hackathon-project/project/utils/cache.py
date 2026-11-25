"""Tiny page-level JSON cache (OCR results are the expensive part)."""
from __future__ import annotations
import hashlib, json
from pathlib import Path


def file_key(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
            from tempfile import NamedTemporaryFile

            p = self._p(*parts); p.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", dir=p.parent, delete=False, encoding="utf-8") as stream:
                temp = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False)
            try:
                temp.replace(p)
            finally:
                temp.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass
