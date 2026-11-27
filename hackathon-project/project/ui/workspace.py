"""Persistent single-document jobs for the local review workspace."""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from dataclasses import replace
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from time import monotonic
from typing import BinaryIO
from uuid import uuid4

from agents.orchestrator import process_pdf, refresh_layout
from config.settings import Settings
from ocr.recognition import ENGINE_MODES
from layout.model import Block, DocIR, ir_from_dict
from utils.logs import AuditLog


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(root: Path):
    db = sqlite3.connect(root / "workspace.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def run_pptx_job(root: Path, input_path: Path, output_folder: Path, job_id: str, settings_json: str) -> None:
    """Run one standalone local PowerPoint proofreading job in the process pool."""
    output_path = output_folder / f"{input_path.stem}_updated.pptx"
    with connect(root) as db:
        db.execute("UPDATE presentation_jobs SET status='running', started=? WHERE id=?",
                   (now(), job_id))

    def progress(stage: dict) -> None:
        with connect(root) as db:
            db.execute("UPDATE presentation_jobs SET progress=? WHERE id=?",
                       (json.dumps(stage), job_id))

    try:
        from powerpoint.proofread import proofread_presentation

        settings = Settings(**json.loads(settings_json))
        result = proofread_presentation(input_path, output_path, settings, progress)
        report = {**result, "filename": input_path.name, "output_filename": output_path.name,
                  "size_bytes": output_path.stat().st_size,
                  "download_url": f"/api/presentations/{job_id}/download"}
        output_folder.mkdir(parents=True, exist_ok=True)
        (output_folder / "processing_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
        with connect(root) as db:
            db.execute("UPDATE presentation_jobs SET status='completed', finished=?, progress=?, result=?, error=NULL "
                       "WHERE id=?", (now(), json.dumps({"stage": "completed", "message": "Presentation ready"}),
                                      json.dumps(report, ensure_ascii=False), job_id))
    except Exception as error:
        logging.getLogger(__name__).exception("Standalone PPTX proofreading failed for job %s", job_id)
        message = (str(error).strip() if isinstance(error, (ValueError, RuntimeError, OSError))
                   else "The presentation could not be proofread. Check the local model and try again.")
        with connect(root) as db:
            db.execute("UPDATE presentation_jobs SET status='failed', finished=?, error=?, "
                       "progress=? WHERE id=?", (now(), message or "The presentation could not be proofread.",
                                                  json.dumps({"stage": "failed", "message": "Proofreading failed"}),
                                                  job_id))


class Workspace:
    def __init__(self, root: Path, settings: Settings, output: Path | None = None):
        self.root = root
        self.output = output if output is not None else root / "output"
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._layout_lock = Lock()
        self.preview_lock = Lock()
        self._layout_health = None
        self.export_progress = {}
        self.export_lock = Lock()
        self.worker = ProcessPoolExecutor(max_workers=max(1, settings.workspace_workers))
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, created TEXT NOT NULL,
                    title TEXT, lang TEXT, approved INTEGER NOT NULL DEFAULT 0,
                    previous_id TEXT REFERENCES documents(id), phase TEXT NOT NULL DEFAULT '1'
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    status TEXT NOT NULL, created TEXT NOT NULL, started TEXT, finished TEXT,
                    done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    error TEXT, result TEXT, settings TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_document ON jobs(document_id, id);
                CREATE TABLE IF NOT EXISTS local_profile (
                    id INTEGER PRIMARY KEY CHECK (id=1), name TEXT NOT NULL
                );
                INSERT OR IGNORE INTO local_profile(id, name) VALUES (1, 'Local reviewer');
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    created TEXT NOT NULL, reviewer TEXT NOT NULL, approved INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ocr_preferences (
                    owner TEXT PRIMARY KEY, engine TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ocr_probes (
                    owner TEXT NOT NULL, engine TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(owner, engine)
                );
                CREATE TABLE IF NOT EXISTS presentation_jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, phase TEXT NOT NULL,
                    filename TEXT NOT NULL, input_path TEXT NOT NULL, output_folder TEXT NOT NULL,
                    status TEXT NOT NULL, created TEXT NOT NULL, started TEXT, finished TEXT,
                    progress TEXT, result TEXT, error TEXT, document_id TEXT REFERENCES documents(id)
                );
            """)
            if "owner" not in {row["name"] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN owner TEXT")
            if "batch_owner" not in {row["name"] for row in db.execute("PRAGMA table_info(local_profile)")}:
                db.execute("ALTER TABLE local_profile ADD COLUMN batch_owner TEXT")
            if "phase" not in {row["name"] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN phase TEXT NOT NULL DEFAULT '1'")
            if "document_id" not in {row["name"] for row in db.execute("PRAGMA table_info(presentation_jobs)")}:
                db.execute("ALTER TABLE presentation_jobs ADD COLUMN document_id TEXT REFERENCES documents(id)")
            db.execute("UPDATE jobs SET status='failed', finished=?, error=? WHERE status IN ('queued','running')",
                       (now(), "Processing was interrupted. Retry to start a new attempt."))
            db.execute("UPDATE presentation_jobs SET status='failed', finished=?, "
                       "error='Proofreading was interrupted. Upload the presentation again to retry.' "
                       "WHERE status IN ('queued','running')", (now(),))

    def db(self):
        return connect(self.root)

    def layout_health(self, retry=False):
        from layout.doclayout import Detector, LayoutError

        def check():
            state = Detector(self.settings, self.root).state
            try:
                with TemporaryDirectory(prefix="layout-check-", dir=self.root) as scratch:
                    with Detector(self.settings, Path(scratch)) as detector:
                        result = detector.start()
            except (LayoutError, OSError) as error:
                result = {**state, "status": "error", "error": str(error)}
            with self._layout_lock:
                self._layout_health = result

        with self._layout_lock:
            if self._layout_health is None or (retry and self._layout_health["status"] != "loading"):
                detector = Detector(self.settings, self.root)
                self._layout_health = {**detector.state, "status": "loading" if self.settings.doclayout_enabled else "disabled"}
                if self.settings.doclayout_enabled:
                    Thread(target=check, daemon=True).start()
            return self._layout_health.copy()

    def source(self, document_id: str) -> Path:
        return self.root / document_id / "source.pdf"

    def _submission_root(self) -> Path:
        if self.output.parent.name in {"phase-1", "phase-2"}:
            return self.output.parent.parent
        return self.output.parent

    def phase_input_dir(self, phase: str | int) -> Path:
        return self._submission_root() / f"phase-{phase}" / "input"

    def phase_output_dir(self, phase: str | int) -> Path:
        return self.phase_output_path(self.output, phase)

    def save_phase_input(self, filename: str, stream: BinaryIO, phase: str | int) -> Path:
        if str(phase) not in {"1", "2"}:
            raise ValueError("Choose Phase 1 or Phase 2.")
        input_dir = self.phase_input_dir(phase)
        input_dir.mkdir(parents=True, exist_ok=True)
        filename = PureWindowsPath(filename).name
        target = input_dir / filename
        if target.exists():
            target = input_dir / f"{Path(filename).stem}_{uuid4().hex[:8]}{Path(filename).suffix}"
        try:
            position = stream.tell()
        except (AttributeError, OSError):
            position = None
        try:
            with target.open("xb") as saved:
                while chunk := stream.read(1024 * 1024):
                    saved.write(chunk)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        finally:
            if position is not None:
                try:
                    stream.seek(position)
                except (AttributeError, OSError):
                    pass
        return target

    @staticmethod
    def phase_output_path(output: Path, phase: str | int) -> Path:
        if output.parent.name not in {"phase-1", "phase-2"} and str(phase) == "1":
            return output
        project_root = output.parent.parent if output.parent.name in {"phase-1", "phase-2"} else output.parent
        return project_root / f"phase-{phase}" / "output"

    def ocr_preference(self, owner: str) -> str:
        with self.db() as db:
            row = db.execute("SELECT engine FROM ocr_preferences WHERE owner=?", (owner,)).fetchone()
        return row["engine"] if row else (self.settings.ocr_backend if self.settings.ocr_backend in ENGINE_MODES else "automatic")

    def set_ocr_preference(self, owner: str, engine: str) -> None:
        if engine not in ENGINE_MODES:
            raise ValueError("Choose Automatic, GLM-OCR or PaddleOCR.")
        with self.db() as db:
            db.execute("INSERT INTO ocr_preferences VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET engine=excluded.engine",
                       (owner, engine))

    def enqueue_presentation(self, filename: str, stream: BinaryIO, owner: str, phase: str,
                             *, document_id: str | None = None) -> str:
        if phase not in {"1", "2"}:
            raise ValueError("Choose Phase 1 or Phase 2.")
        input_path = self.save_phase_input(filename, stream, phase)
        job_id = uuid4().hex
        output_folder = self.phase_output_dir(phase) / "PowerPoint" / input_path.stem / job_id
        with self.db() as db:
            db.execute("INSERT INTO presentation_jobs(id, owner, phase, filename, input_path, output_folder, "
                       "status, created, document_id) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                       (job_id, owner, phase, filename, str(input_path), str(output_folder), now(), document_id))
        try:
            future = self.worker.submit(run_pptx_job, self.root, input_path, output_folder,
                                        job_id, json.dumps(self.settings.to_dict()))
        except RuntimeError:
            self._presentation_worker_failed(job_id)
        else:
            future.add_done_callback(lambda task: self._check_presentation_worker(task, job_id))
        return job_id

    def _check_presentation_worker(self, task: Future, job_id: str) -> None:
        if task.cancelled() or task.exception() is not None:
            self._presentation_worker_failed(job_id)

    def _presentation_worker_failed(self, job_id: str) -> None:
        with self.db() as db:
            db.execute("UPDATE presentation_jobs SET status='failed', finished=?, error=?, progress=? WHERE id=?",
                       (now(), "The proofreading worker stopped. Restart the workspace and upload again.",
                        json.dumps({"stage": "failed", "message": "Proofreading worker stopped"}), job_id))

    @staticmethod
    def _public_presentation(row: sqlite3.Row) -> dict:
        item = dict(row)
        item.pop("owner", None)
        item.pop("input_path", None)
        item.pop("output_folder", None)
        progress = item.pop("progress", None)
        result = item.pop("result", None)
        item["progress"] = json.loads(progress) if progress else None
        item["result"] = json.loads(result) if result else None
        return item

    def get_presentation(self, job_id: str, owner: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM presentation_jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        return self._public_presentation(row) if row else None

    def presentation_source(self, job_id: str, owner: str) -> Path | None:
        with self.db() as db:
            row = db.execute("SELECT phase, input_path FROM presentation_jobs WHERE id=? AND owner=?",
                             (job_id, owner)).fetchone()
        if row:
            path = Path(row["input_path"]).resolve()
            if path.is_relative_to(self.phase_input_dir(row["phase"]).resolve()) and path.is_file():
                return path
        return None

    def presentation_download(self, job_id: str, owner: str) -> Path | None:
        with self.db() as db:
            row = db.execute("SELECT phase, output_folder, status, result FROM presentation_jobs "
                             "WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        return self._presentation_output(row, json.loads(row["result"])) if row and row["result"] else None

    def _presentation_output(self, row, result: dict) -> Path | None:
        filename = result.get("output_filename")
        if row["status"] != "completed" or not result.get("validation", {}).get("valid") or not filename:
            return None
        folder = Path(row["output_folder"]).resolve()
        output = (folder / filename).resolve()
        phase_output = self.phase_output_dir(row["phase"]).resolve()
        return output if folder.is_relative_to(phase_output) and output.is_relative_to(folder) and output.is_file() else None

    def presentations(self, owner: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT * FROM presentation_jobs WHERE owner=? ORDER BY created DESC LIMIT 100",
                              (owner,)).fetchall()
        items = []
        for row in rows:
            item = self._public_presentation(row)
            if item["result"] and self._presentation_output(row, item["result"]):
                item["pptx_download_url"] = f"/api/presentations/{item['id']}/download"
            items.append(item)
        return items

    def upload(self, filename: str, stream: BinaryIO, previous_id: str | None, owner: str,
               *, phase: str = "1", engine: str | None = None, language: str | None = None,
               defer: bool = False) -> str:
        if phase not in {"1", "2"}:
            raise ValueError("Choose Phase 1 or Phase 2.")
        document_id = uuid4().hex
        source = self.source(document_id)
        filename = Path(filename).name
        input_dir = self.phase_input_dir(phase)
        input_path = input_dir / filename
        if input_path.exists():
            input_path = input_dir / f"{Path(filename).stem}_{document_id[:8]}{Path(filename).suffix}"
        with self.db() as db:
            db.execute("INSERT INTO documents(id, filename, created, previous_id, owner, phase) VALUES (?, ?, ?, ?, ?, ?)",
                       (document_id, filename, now(), previous_id, owner, phase))
            source.parent.mkdir()
            input_dir.mkdir(parents=True, exist_ok=True)
            try:
                with source.open("xb") as output, input_path.open("xb") as phase_input:
                    while chunk := stream.read(1024 * 1024):
                        output.write(chunk)
                        phase_input.write(chunk)
            except OSError:
                source.unlink(missing_ok=True)
                source.parent.rmdir()
                input_path.unlink(missing_ok=True)
                raise
            job = self._create_job(db, document_id, engine, language, status="uploaded" if defer else "queued")
        if not defer:
            self._submit(document_id, job, phase=phase)
        return document_id

    def owns(self, document_id: str, owner: str | None) -> bool:
        with self.db() as db:
            return db.execute("SELECT 1 FROM documents WHERE id=? AND owner=?", (document_id, owner)).fetchone() is not None

    def owns_batch(self, owner: str | None) -> bool:
        with self.db() as db:
            return db.execute("SELECT 1 FROM local_profile WHERE id=1 AND batch_owner=?", (owner,)).fetchone() is not None

    def documents(self, owner: str) -> list[dict]:
        with self.db() as db:
            # Workspaces created before browser ownership were local to one user.
            db.execute("UPDATE local_profile SET batch_owner=? WHERE id=1 AND batch_owner IS NULL", (owner,))
            db.execute("UPDATE documents SET owner=? WHERE owner IS NULL", (owner,))
            return [dict(row) for row in db.execute("""
                SELECT d.id, d.filename, d.created, d.previous_id, d.approved, d.phase,
                       j.status, j.done, j.total
                FROM documents d JOIN jobs j ON j.id =
                    (SELECT MAX(id) FROM jobs WHERE document_id=d.id)
                WHERE d.owner=? ORDER BY d.created DESC
            """, (owner,))]

    def get(self, document_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if row is None:
                return None
            jobs = db.execute("""
                SELECT id, status, created, started, finished, done, total, error, settings,
                       CASE WHEN status='running' OR json_type(result, '$.stage')='text' THEN result
                            ELSE json_extract(result, '$.processing') END AS stage_json
                FROM jobs WHERE document_id=? ORDER BY id DESC
            """, (document_id,))
            review = db.execute("""
                SELECT created, reviewer, approved FROM reviews WHERE document_id=?
                ORDER BY id DESC LIMIT 1
            """, (document_id,)).fetchone()
            public_jobs = []
            for job in jobs:
                state = dict(job)
                stage_data = state.pop("stage_json")
                config = json.loads(state.pop("settings"))
                state["ocr_selection"] = {key: config.get(key) for key in (
                    "ocr_backend", "ocr_automatic_primary", "ocr_automatic_secondary", "ocr_automatic_tertiary",
                    "glm_ocr_model", "paddle_vl_model", "paddle_ocr_lang", "paddle_ocr_unknown_lang")}
                if stage_data:
                    state["processing"] = json.loads(stage_data)
                    if state["status"] == "failed":
                        for phase in state["processing"].get("phases", {}).values():
                            if phase["status"] == "running":
                                elapsed = datetime.fromisoformat(state["finished"]) - datetime.fromisoformat(phase["at"])
                                phase.update(status="failed", seconds=max(0, elapsed.total_seconds()))
                public_jobs.append(state)
            document = dict(row)
            document.pop("owner")
            return {**document, "jobs": public_jobs, "review": dict(review) if review else None}

    def enqueue(self, document_id: str, *, layout_only: bool = False, engine: str | None = None,
                language: str | None = None) -> bool:
        with self.db() as db:
            previous = None
            document = db.execute("SELECT phase FROM documents WHERE id=?", (document_id,)).fetchone()
            if document is None:
                return False
            if layout_only:
                previous = db.execute("""SELECT id, settings FROM jobs WHERE document_id=? AND status='completed'
                                         ORDER BY id DESC LIMIT 1""", (document_id,)).fetchone()
                if previous is None:
                    return False
            if not layout_only:
                pending = db.execute("UPDATE jobs SET status='queued' WHERE document_id=? AND status='uploaded' "
                                     "RETURNING id", (document_id,)).fetchone()
            else:
                pending = None
            job = pending["id"] if pending else self._create_job(db, document_id, engine, language)
            if job is None:
                return False
            if previous is not None:
                db.execute("UPDATE jobs SET settings=? WHERE id=?", (previous["settings"], job))
            db.execute("UPDATE documents SET approved=0 WHERE id=?", (document_id,))
        self._submit(document_id, job, previous["id"] if previous is not None else None,
                     phase=document["phase"])
        return True

    def _create_job(self, db: sqlite3.Connection, document_id: str, engine: str | None = None,
                    language: str | None = None, *, status: str = "queued") -> int | None:
        settings = replace(self.settings, ocr_backend=engine or self.settings.ocr_backend,
                           paddle_ocr_lang=language if language is not None else self.settings.paddle_ocr_lang)
        cursor = db.execute("""
            INSERT INTO jobs(document_id, status, created, settings)
            SELECT ?, ?, ?, ? WHERE NOT EXISTS(
                SELECT 1 FROM jobs WHERE document_id=? AND status IN ('queued','running'))
        """, (document_id, status, now(), json.dumps(settings.to_dict()), document_id))
        return cursor.lastrowid if cursor.rowcount else None

    def _submit(self, document_id: str, job: int, layout_from: int | None = None, *, phase: str = "1") -> None:
        try:
            future = self.worker.submit(self._process, self.root, self.output, document_id, job, layout_from, phase)
        except RuntimeError:
            self._worker_failed(job)
        else:
            future.add_done_callback(lambda task: self._check_worker(task, job))

    def _check_worker(self, task: Future, job: int) -> None:
        if task.cancelled() or task.exception() is not None:
            self._worker_failed(job)

    def _worker_failed(self, job: int) -> None:
        with self.db() as db:
            db.execute("UPDATE jobs SET status='failed', finished=?, error=? WHERE id=?",
                       (now(), "The processing worker stopped. Restart the workspace and retry.", job))

    @staticmethod
    def _process(root: Path, output: Path, document_id: str, job: int, layout_from: int | None = None,
                 phase: str = "1") -> None:
        run = root / document_id / str(job)
        with connect(root) as db:
            settings_json = db.execute("SELECT settings FROM jobs WHERE id=?", (job,)).fetchone()[0]
            previous_result = (db.execute("SELECT result FROM jobs WHERE id=? AND document_id=? AND status='completed'",
                                          (layout_from, document_id)).fetchone() if layout_from is not None else None)
            db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (now(), job))
        settings = Settings(**json.loads(settings_json))
        history = deque(maxlen=30)
        page_seconds = {}
        phases = {}
        clocks = {}
        snapshot = {}

        def progress(done: int, total: int) -> None:
            with connect(root) as db:
                db.execute("UPDATE jobs SET done=?, total=? WHERE id=?", (done, total, job))

        def processing(stage: dict) -> None:
            nonlocal snapshot
            if state := stage.get("state"):
                name = stage["stage"]
                phase = {"status": state, "at": now()}
                if state == "running":
                    clocks[name] = monotonic()
                else:
                    phase["seconds"] = round(monotonic() - clocks.pop(name, monotonic()), 2)
                phases[name] = phase
            if stage.get("stage") == "extracted" and stage.get("elapsed_seconds") is not None:
                page_seconds[stage["page"]] = {"seconds": stage["elapsed_seconds"],
                                               "ocr": stage.get("ocr_required", False)}
            history.append({**stage, "at": now()})
            recent = list(history)
            snapshot = {**stage, "history": recent, "page_seconds": list(page_seconds.values()),
                        "phases": phases, "last_ocr_attempt": next((item for item in reversed(recent)
                                                                   if item["stage"] == "ocr_attempt"), None)}
            with connect(root) as db:
                db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(snapshot), job))

        result = {}
        error = None
        try:
            audit = AuditLog(run / "audit.jsonl")
            if layout_from is not None:
                old_ir = run.parent / str(layout_from) / "ir" / "source.json"
                if previous_result is None:
                    raise ValueError("The saved OCR attempt is unavailable.")
                ir = ir_from_dict(json.loads(old_ir.read_text(encoding="utf-8")))
                audit.event("layout", "refresh_started", previous_attempt=layout_from)
                result = refresh_layout(run.parent / "source.pdf", ir, settings, run / "output", run, audit,
                                        json.loads(previous_result[0]), progress, processing)
            else:
                result = process_pdf(run.parent / "source.pdf", settings, run / "output", run, audit,
                                     progress=progress, processing=processing, document_id=document_id)
            if result.get("ok"):
                from layout.model import ir_from_dict
                from reporting.exports import output_stem, publish_document

                ir = ir_from_dict(json.loads((run / "ir" / "source.json").read_text(encoding="utf-8")))
                ir.doc_id = document_id
                info = connect(root)
                with info as db:
                    filename = db.execute("SELECT filename FROM documents WHERE id=?", (document_id,)).fetchone()[0]
                export_id = uuid4()
                export = publish_document(
                    Workspace.phase_output_path(output, phase) / output_stem(filename) / export_id.hex,
                    output_stem(filename), ir, settings, result,
                    {"id": str(export_id), "document_id": document_id, "filename": filename,
                     "phase": phase, "mode": "pdf_processing", "created": now(), "job_id": job},
                    None, xml_only=True,
                    progress=lambda stages: processing({"stage": "validation",
                                                        "state": stages.get("validation", {}).get("status"),
                                                        "export_phases": stages}),
                )
                result["submission_export"] = {
                    "phase": phase, "output_folder": export["output_folder"], "outputs": export["outputs"],
                    "status": export["status"], "xml_profile": export["xml_profile"],
                    "error": export.get("error", ""),
                }
                if not export["outputs"].get("xml"):
                    error = "PDF extraction completed, but the XML could not be saved to the phase output folder."
            if result.get("validation", {}).get("issues"):
                error = "The extracted document failed XML validation."
            elif not result["ok"]:
                error = ("PDF is encrypted and requires a password."
                         if result.get("error", "").startswith("PDF is encrypted")
                         else "The PDF could not be processed. Check that the original opens.")
            elif any(page["health"] == "error" for page in result["page_log"]):
                error = "One or more PDF pages could not be read."
        except (ValueError, RuntimeError) as failure:
            logging.getLogger(__name__).exception("Processing failed for document %s, job %s", document_id, job)
            error = str(failure) or "Processing failed. Check the local runtime and retry."
        except Exception:
            logging.getLogger(__name__).exception("Processing failed for document %s, job %s", document_id, job)
            error = "Processing failed. Check the local runtime and retry."
        for name, started in clocks.items():
            phases[name] = {"status": "failed" if error else "completed", "at": now(),
                            "seconds": round(monotonic() - started, 2)}
        result["processing"] = snapshot
        with connect(root) as db:
            db.execute("UPDATE jobs SET status=?, finished=?, error=?, result=? WHERE id=?",
                       ("failed" if error else "completed", now(), error, json.dumps(result), job))

    def extracted(self, document_id: str, *, reviewed: bool = True) -> tuple[DocIR, Settings, dict] | None:
        with self.db() as db:
            row = db.execute("""
                SELECT j.id, j.settings, j.result, d.title, d.lang FROM jobs j
                JOIN documents d ON d.id=j.document_id
                WHERE j.document_id=? AND j.status='completed' ORDER BY j.id DESC LIMIT 1
            """, (document_id,)).fetchone()
        if row is None:
            return None
        path = self.root / document_id / str(row["id"]) / "ir" / "source.json"
        ir = ir_from_dict(json.loads(path.read_text(encoding="utf-8")))
        ir.doc_id = document_id
        if reviewed and row["lang"]:
            ir.lang = row["lang"]
        if reviewed and row["title"] is not None:
            title = next((block for page in ir.pages for block in page.blocks if block.kind == "title"), None)
            if title is not None:
                if title.text != row["title"]:
                    title.source = "reviewed"
                    title.text, title.spans = row["title"], []
            elif row["title"]:
                ir.pages[0].blocks.insert(0, Block(tag="h1", kind="title", text=row["title"], source="reviewed"))
        return ir, Settings(**json.loads(row["settings"])), json.loads(row["result"])

    def review(self, document_id: str, title: str, lang: str, approved: bool) -> bool:
        with self.db() as db:
            updated = db.execute("""
                UPDATE documents SET title=?, lang=?, approved=? WHERE id=? AND
                (SELECT status FROM jobs WHERE document_id=? ORDER BY id DESC LIMIT 1)='completed'
            """, (title, lang, approved, document_id, document_id))
            if not updated.rowcount:
                return False
            db.execute("""
                INSERT INTO reviews(document_id, created, reviewer, approved)
                SELECT ?, ?, name, ? FROM local_profile WHERE id=1
            """, (document_id, now(), approved))
            return True

    def reviewer(self, name: str | None = None) -> str:
        with self.db() as db:
            if name is not None:
                db.execute("UPDATE local_profile SET name=? WHERE id=1", (name,))
            return db.execute("SELECT name FROM local_profile WHERE id=1").fetchone()[0]
