"""Manage the isolated CPU detector and convert its output to PDF page points."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
from threading import Thread

import pymupdf

from utils import execution_audit as audit

ROOT = Path(__file__).resolve().parents[2]
MODEL = "DocLayout-YOLO"


class LayoutError(RuntimeError):
    pass


class Detector:
    def __init__(self, settings, scratch: Path):
        self.settings = settings
        self.scratch = scratch
        self.python = (ROOT / settings.doclayout_python).resolve()
        self.repo = (ROOT / settings.doclayout_repo).resolve()
        self.checkpoint = (ROOT / settings.doclayout_checkpoint).resolve()
        self.process = None
        self.state = {"model": MODEL, "device": None, "checkpoint": self.checkpoint.name,
                      "checkpoint_available": self.checkpoint.is_file(), "role": "Layout Detection"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start(self):
        if not self.settings.doclayout_enabled:
            return {**self.state, "status": "disabled"}
        if self.process is not None:
            return self.state
        for valid, message in (
            (self.state["checkpoint_available"], f"Checkpoint missing: {self.checkpoint.name}"),
            (self.python.is_file(), "DocLayout-YOLO Python interpreter is unavailable."),
            ((self.repo / "ultralytics" / "__init__.py").is_file(), "DocLayout-YOLO repository is unavailable."),
        ):
            if not valid:
                raise LayoutError(message)
        env = {**os.environ, "YOLO_CONFIG_DIR": str(self.scratch), "YOLO_AUTOINSTALL": "False",
               "YOLO_OFFLINE": "True", "PYTHONIOENCODING": "utf-8"}
        self.responses = Queue()
        try:
            self.process = subprocess.Popen(
                [str(self.python), "-B", "-u", str(Path(__file__).with_name("doclayout_worker.py")),
                 str(self.repo), str(self.checkpoint)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8", errors="replace",
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            Thread(target=self._read, args=(self.process.stdout, self.responses), daemon=True).start()
            ready = self._receive()
            if ready.get("status") != "ready" or ready.get("device") != "cpu" or not ready.get("classes"):
                raise LayoutError("Unexpected initialization response from DocLayout-YOLO.")
            self.state.update(ready)
            return self.state
        except (LayoutError, OSError) as error:
            self.close()
            raise LayoutError(str(error)) from error

    @staticmethod
    def _read(stream, responses):
        try:
            for line in stream:
                responses.put(line)
        finally:
            responses.put(None)

    def _receive(self):
        try:
            line = self.responses.get(timeout=max(1, self.settings.doclayout_timeout_s))
        except Empty as error:
            raise LayoutError("DocLayout-YOLO timed out.") from error
        if line is None:
            raise LayoutError("The DocLayout-YOLO worker stopped.")
        try:
            response = json.loads(line)
        except ValueError as error:
            raise LayoutError("The DocLayout-YOLO worker sent invalid JSON.") from error
        if not isinstance(response, dict):
            raise LayoutError("The DocLayout-YOLO worker sent an invalid response.")
        if "error" in response:
            raise LayoutError(response["error"])
        return response

    def detect(self, rendered):
        with audit.execution("page_layout", "doclayout-yolo", self.checkpoint.name, page=rendered["page"],
                             input_type="page_image",
                             input_reference=f"pdf/page/{rendered['page']}") as event:
            request = {"image": str(rendered["path"]), "page": rendered["page"],
                       "size": self.settings.doclayout_image_size, "confidence": self.settings.doclayout_confidence}
            try:
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
                if event is not None:
                    event["request_status"] = "sent"
                result = self._receive()
                if result.get("page") != request["page"] or result.get("shape") != rendered["shape"]:
                    raise LayoutError("The layout response does not match the rendered page.")
                regions = convert_regions(result["regions"], rendered, self.state["classes"])
            except (LayoutError, OSError, ValueError, TypeError, KeyError) as error:
                self.close()
                raise LayoutError(f"Page {rendered['page']} layout failed: {error}") from error
            audit.response(event, result, executed=True)
            return regions

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def render(page, folder, image_size):
    if not 32 <= image_size <= 2048:
        raise LayoutError("DocLayout-YOLO image size must be between 32 and 2048 pixels.")
    rect = page.rect
    scale = image_size / max(rect.width, rect.height)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    path = folder / f"page-{page.number + 1}.png"
    pixmap.save(path)
    return {"path": path, "page": page.number + 1, "shape": [pixmap.height, pixmap.width],
            "width": rect.width, "height": rect.height, "derotation": page.derotation_matrix}


def convert_regions(detections, rendered, classes):
    height, width = rendered["shape"]
    sx, sy = rendered["width"] / width, rendered["height"] / height
    regions = []
    for index, item in enumerate(detections, 1):
        x0, y0, x1, y1 = item["pixels"]
        confidence = item["confidence"]
        label = item["class_name"]
        if (classes.get(str(item["class_id"])) != label or not 0 <= confidence <= 1
                or not all(math.isfinite(v) for v in (x0, y0, x1, y1))
                or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)):
            raise LayoutError("DocLayout-YOLO returned an invalid class, confidence or box.")
        box = pymupdf.Rect(x0 * sx, y0 * sy, x1 * sx, y1 * sy)
        regions.append({"id": f"p{rendered['page']}-r{index}", "page": rendered["page"], "class_name": label,
                        "class_id": item["class_id"], "confidence": confidence, "source": MODEL,
                        "display_bbox": list(box), "bbox": list(box * rendered["derotation"])})
    return regions
