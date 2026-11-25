"""Persistent single-document jobs for the local review workspace."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from agents.orchestrator import process_pdf, refresh_layout
from config.settings import Settings
from ocr.recognition import ENGINE_MODES
from layout.model import Block, DocIR, ir_from_dict
from utils.logs import AuditLog
from xml_engine.serializer import to_xml
from xml_engine.validate import validate_xml


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


class Workspace:
    def __init__(self, root: Path, settings: Settings, output: Path | None = None):
        self.root = root
        self.output = output if output is not None else root / "output"
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.worker = ProcessPoolExecutor(max_workers=max(1, settings.workspace_workers))
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, created TEXT NOT NULL,
                    title TEXT, lang TEXT, approved INTEGER NOT NULL DEFAULT 0,
                    previous_id TEXT REFERENCES documents(id)
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
            """)
            if "owner" not in {row["name"] for row in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN owner TEXT")
            if "batch_owner" not in {row["name"] for row in db.execute("PRAGMA table_info(local_profile)")}:
                db.execute("ALTER TABLE local_profile ADD COLUMN batch_owner TEXT")
            db.execute("UPDATE jobs SET status='failed', finished=?, error=? WHERE status IN ('queued','running')",
                       (now(), "Processing was interrupted. Retry to start a new attempt."))

    def db(self):
        return connect(self.root)

    def source(self, document_id: str) -> Path:
        return self.root / document_id / "source.pdf"

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

    def upload(self, filename: str, stream: BinaryIO, previous_id: str | None, owner: str,
               *, engine: str | None = None, language: str | None = None) -> str:
        document_id = uuid4().hex
        source = self.source(document_id)
        with self.db() as db:
            db.execute("INSERT INTO documents(id, filename, created, previous_id, owner) VALUES (?, ?, ?, ?, ?)",
                       (document_id, filename, now(), previous_id, owner))
            source.parent.mkdir()
            try:
                with source.open("xb") as output:
                    while chunk := stream.read(1024 * 1024):
                        output.write(chunk)
            except OSError:
                source.unlink(missing_ok=True)
                source.parent.rmdir()
                raise
            job = self._create_job(db, document_id, engine, language)
        self._submit(document_id, job)
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
                SELECT d.id, d.filename, d.created, d.previous_id, d.approved,
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
                       CASE WHEN status='running' THEN result END AS stage_json
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
                    "ocr_backend", "ocr_automatic_primary", "glm_ocr_model", "paddle_ocr_lang", "paddle_ocr_unknown_lang")}
                if stage_data:
                    state["processing"] = json.loads(stage_data)
                public_jobs.append(state)
            document = dict(row)
            document.pop("owner")
            return {**document, "jobs": public_jobs, "review": dict(review) if review else None}

    def enqueue(self, document_id: str, *, layout_only: bool = False, engine: str | None = None,
                language: str | None = None) -> bool:
        with self.db() as db:
            previous = None
            if layout_only:
                previous = db.execute("""SELECT id, settings FROM jobs WHERE document_id=? AND status='completed'
                                         ORDER BY id DESC LIMIT 1""", (document_id,)).fetchone()
                if previous is None:
                    return False
            job = self._create_job(db, document_id, engine, language)
            if job is None:
                return False
            if previous is not None:
                db.execute("UPDATE jobs SET settings=? WHERE id=?", (previous["settings"], job))
            db.execute("UPDATE documents SET approved=0 WHERE id=?", (document_id,))
        self._submit(document_id, job, previous["id"] if previous is not None else None)
        return True

    def _create_job(self, db: sqlite3.Connection, document_id: str, engine: str | None = None,
                    language: str | None = None) -> int | None:
        settings = replace(self.settings, ocr_backend=engine or self.settings.ocr_backend,
                           paddle_ocr_lang=language if language is not None else self.settings.paddle_ocr_lang)
        cursor = db.execute("""
            INSERT INTO jobs(document_id, status, created, settings)
            SELECT ?, 'queued', ?, ? WHERE NOT EXISTS(
                SELECT 1 FROM jobs WHERE document_id=? AND status IN ('queued','running'))
        """, (document_id, now(), json.dumps(settings.to_dict()), document_id))
        return cursor.lastrowid if cursor.rowcount else None

    def _submit(self, document_id: str, job: int, layout_from: int | None = None) -> None:
        try:
            future = self.worker.submit(self._process, self.root, document_id, job, layout_from)
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
    def _process(root: Path, document_id: str, job: int, layout_from: int | None = None) -> None:
        run = root / document_id / str(job)
        with connect(root) as db:
            settings_json = db.execute("SELECT settings FROM jobs WHERE id=?", (job,)).fetchone()[0]
            previous_result = (db.execute("SELECT result FROM jobs WHERE id=? AND document_id=? AND status='completed'",
                                          (layout_from, document_id)).fetchone() if layout_from is not None else None)
            db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (now(), job))
        settings = Settings(**json.loads(settings_json))

        def progress(done: int, total: int) -> None:
            with connect(root) as db:
                db.execute("UPDATE jobs SET done=?, total=? WHERE id=?", (done, total, job))

        def processing(stage: dict) -> None:
            with connect(root) as db:
                db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(stage), job))

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
            error = str(failure) if layout_from is not None else "Processing failed. Check the local runtime and retry."
        except Exception:
            logging.getLogger(__name__).exception("Processing failed for document %s, job %s", document_id, job)
            error = "Processing failed. Check the local runtime and retry."
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

    def xml(self, document_id: str) -> tuple[bytes, dict] | None:
        extracted = self.extracted(document_id)
        if extracted is None:
            return None
        ir, settings, _ = extracted
        data = to_xml(ir, settings)
        return data, validate_xml(data, len(ir.pages), settings)
