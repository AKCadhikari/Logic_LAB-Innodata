"""Folder intake and a durable, sequential queue for existing paired workspace jobs."""
from __future__ import annotations

from collections import Counter, defaultdict
import logging
from os import link, replace
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ui.workspace import now
from xml_engine.assets import relative_path

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FOLDERS = 500
MAX_FILES = 2000
LOGGER = logging.getLogger(__name__)


def intake_folders(folders):
    if not isinstance(folders, list) or not 1 <= len(folders) <= MAX_FOLDERS:
        raise ValueError(f"Select between 1 and {MAX_FOLDERS} folders.")
    names = Counter(folder.get("name", "").casefold() for folder in folders
                    if isinstance(folder, dict) and isinstance(folder.get("name"), str))
    count = 0
    for folder in folders:
        name = folder.get("name") if isinstance(folder, dict) else None
        files = folder.get("files") if isinstance(folder, dict) else None
        if not isinstance(name, str) or not name or len(name) > 255 or not isinstance(files, list):
            raise ValueError("Every folder needs a name and a list of files.")
        issue = None
        if "/" in name or not relative_path(name):
            issue = "This folder name cannot be used for a Windows output folder."
        elif names[name.casefold()] > 1:
            issue = "More than one selected folder has this name."
        count += len(files)
        if count > MAX_FILES:
            raise ValueError(f"Select at most {MAX_FILES} files in one batch.")
        entries = []
        for file in files:
            filename = file.get("name") if isinstance(file, dict) else None
            size = file.get("size") if isinstance(file, dict) else None
            if (not isinstance(filename, str) or not 1 <= len(filename) <= 255
                    or type(size) is not int or size < 0):
                raise ValueError("Each file needs a name and size.")
            kind = Path(filename).suffix.lower().lstrip(".")
            status, error = "pending", None
            if kind not in {"pdf", "pptx"} or file.get("nested"):
                kind, status, error = "unexpected", "invalid", "Unexpected file or nested folder."
            elif "/" in filename or not relative_path(filename):
                status, error = "invalid", "This filename cannot be used on Windows."
            elif not 0 < size < MAX_FILE_BYTES:
                status, error = "invalid", "The file must be nonempty and smaller than 64 MB."
            entries.append((filename, kind, size, status, error))
        yield name, issue, entries


def pairing(job, files):
    groups = {kind: [file for file in files if file["kind"] == kind] for kind in ("pdf", "pptx")}
    issues = [job["folder_issue"]] if job["folder_issue"] else []
    for kind, candidates in groups.items():
        if not candidates:
            issues.append(f"Missing {kind.upper()}.")
        elif len(candidates) > 1:
            issues.append(f"Multiple {kind.upper()} files. Keep one file of each type and upload the folder again.")
    if any(file["status"] == "invalid" for file in files):
        issues.append("Review invalid or unexpected files.")
    pending = any(file["status"] == "pending" for file in files)
    return groups, issues, pending


class BulkQueue:
    def __init__(self, workspace):
        self.ws = workspace
        with workspace.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bulk_batches (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, phase TEXT NOT NULL, ocr_engine TEXT,
                    ocr_language TEXT, created TEXT NOT NULL, started TEXT
                );
                CREATE INDEX IF NOT EXISTS bulk_batches_owner ON bulk_batches(owner, created);
                CREATE TABLE IF NOT EXISTS bulk_jobs (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES bulk_batches(id),
                    folder TEXT NOT NULL, folder_issue TEXT, pending TEXT, queued_at TEXT,
                    active INTEGER NOT NULL DEFAULT 0, canceled INTEGER NOT NULL DEFAULT 0,
                    ocr_engine TEXT, ocr_language TEXT, error TEXT, finished TEXT
                );
                CREATE INDEX IF NOT EXISTS bulk_jobs_batch ON bulk_jobs(batch_id);
                CREATE UNIQUE INDEX IF NOT EXISTS bulk_jobs_one_active ON bulk_jobs(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS bulk_files (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES bulk_jobs(id),
                    name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS bulk_files_job ON bulk_files(job_id);
            """)
            db.execute("UPDATE bulk_jobs SET active=0 WHERE active=1")

    def contains(self, document_id):
        with self.ws.db() as db:
            return db.execute("SELECT 1 FROM bulk_jobs WHERE id=?", (document_id,)).fetchone() is not None

    def owns_job(self, batch_id, document_id, owner):
        with self.ws.db() as db:
            return db.execute("""
                SELECT 1 FROM bulk_jobs j JOIN bulk_batches b ON b.id=j.batch_id
                WHERE b.id=? AND j.id=? AND b.owner=?
            """, (batch_id, document_id, owner)).fetchone() is not None

    def batches(self, owner):
        with self.ws.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT b.id,b.phase,b.created,b.started,COUNT(j.id) AS folders
                FROM bulk_batches b JOIN bulk_jobs j ON j.batch_id=b.id
                WHERE b.owner=? GROUP BY b.id ORDER BY b.created DESC
            """, (owner,))]

    def create(self, batch_id, owner, phase, engine, language, folders):
        folders = list(intake_folders(folders))
        with self.ws.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT owner FROM bulk_batches WHERE id=?", (batch_id,)).fetchone()
            if existing:
                return existing["owner"] == owner
            db.execute("INSERT INTO bulk_batches VALUES (?,?,?,?,?,?,NULL)",
                       (batch_id, owner, phase, engine, language, now()))
            for name, issue, files in folders:
                job_id = uuid4().hex
                db.execute("INSERT INTO bulk_jobs(id,batch_id,folder,folder_issue) VALUES (?,?,?,?)",
                           (job_id, batch_id, name, issue))
                db.executemany("INSERT INTO bulk_files VALUES (?,?,?,?,?,?,?)",
                               [(uuid4().hex, job_id, *entry) for entry in files])
        return True

    def _file(self, batch_id, file_id, owner):
        with self.ws.db() as db:
            return db.execute("""
                SELECT f.* FROM bulk_files f JOIN bulk_jobs j ON j.id=f.job_id
                JOIN bulk_batches b ON b.id=j.batch_id WHERE b.id=? AND f.id=? AND b.owner=?
            """, (batch_id, file_id, owner)).fetchone()

    def file_path(self, batch_id, file_id):
        return self.ws.root / "bulk-intake" / batch_id / file_id

    def save_file(self, batch_id, file_id, owner, stream, validate):
        file = self._file(batch_id, file_id, owner)
        if not file:
            return None
        if file["status"] != "pending":
            return file["status"]
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        error = None
        stage = None
        target = self.file_path(batch_id, file_id)
        try:
            if size != file["size"]:
                error = "The upload size differs from the selected file."
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(dir=target.parent, suffix="." + file["kind"], delete=False) as temp:
                    stage = Path(temp.name)
                    copyfileobj(stream, temp, 1024 * 1024)
                error = validate(stage, file["kind"])
            with self.ws.db() as db:
                db.execute("BEGIN IMMEDIATE")
                status = db.execute("SELECT status FROM bulk_files WHERE id=?", (file_id,)).fetchone()["status"]
                if status != "pending":
                    return status
                if not error:
                    replace(stage, target)
                status = "invalid" if error else "valid"
                db.execute("UPDATE bulk_files SET status=?,error=? WHERE id=?", (status, error, file_id))
            return status
        finally:
            if stage:
                stage.unlink(missing_ok=True)

    @staticmethod
    def _files(db, batch_id):
        grouped = defaultdict(list)
        for row in db.execute("""
            SELECT f.* FROM bulk_files f JOIN bulk_jobs j ON j.id=f.job_id
            WHERE j.batch_id=? ORDER BY f.rowid
        """, (batch_id,)):
            grouped[row["job_id"]].append(dict(row))
        return grouped

    def start(self, batch_id, owner):
        with self.ws.db() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute("SELECT started FROM bulk_batches WHERE id=? AND owner=?",
                               (batch_id, owner)).fetchone()
            if not batch:
                return None
            if not batch["started"]:
                ready = []
                files = self._files(db, batch_id)
                for job in db.execute("SELECT * FROM bulk_jobs WHERE batch_id=?", (batch_id,)):
                    _, issues, pending = pairing(job, files[job["id"]])
                    if pending:
                        raise ValueError("Finish uploading and checking the selected files before starting.")
                    if not issues:
                        ready.append(job["id"])
                if not ready:
                    raise ValueError("This batch has no complete pairs.")
                started = now()
                db.execute("UPDATE bulk_batches SET started=? WHERE id=?", (started, batch_id))
                db.executemany("UPDATE bulk_jobs SET pending='start',queued_at=? WHERE id=?",
                               [(started, job) for job in ready])
        self.kick()
        return True

    def cancel(self, batch_id, owner, job_id=None):
        with self.ws.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM bulk_batches WHERE id=? AND owner=?", (batch_id, owner)).fetchone():
                return None
            held = db.execute("""
                UPDATE jobs SET status=CASE WHEN json_extract(result,'$.final_export.status')='manual_review_required'
                    THEN 'review' ELSE 'canceled' END,finished=?
                WHERE status='queued' AND document_id IN (
                    SELECT id FROM bulk_jobs WHERE batch_id=? AND (? IS NULL OR id=?) AND active=1)
                RETURNING document_id
            """, (now(), batch_id, job_id, job_id)).fetchall()
            db.executemany("UPDATE bulk_jobs SET canceled=1,finished=? WHERE id=?",
                           [(now(), row["document_id"]) for row in held])
            pending = db.execute("""
                UPDATE bulk_jobs SET pending=NULL,canceled=1,finished=?
                WHERE batch_id=? AND (? IS NULL OR id=?) AND pending IS NOT NULL
            """, (now(), batch_id, job_id, job_id)).rowcount
            return len(held) + pending

    def retry(self, document_id, *, engine=None, language=None, missing_outputs=False):
        with self.ws.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT b.started AS batch_started,k.pending,k.active,k.canceled,k.error,j.status
                FROM bulk_jobs k JOIN bulk_batches b ON b.id=k.batch_id
                LEFT JOIN jobs j ON j.id=(SELECT MAX(id) FROM jobs WHERE document_id=k.id)
                WHERE k.id=?
            """, (document_id,)).fetchone()
            if (not row or not row["batch_started"] or row["pending"]
                    or row["active"] and row["status"] in {"queued", "running"}
                    or not (row["canceled"] or row["error"] or row["status"] in {"failed", "review"}
                            or missing_outputs and row["status"] == "completed")):
                return False
            db.execute("""
                UPDATE bulk_jobs SET pending='retry',queued_at=?,canceled=0,error=NULL,finished=NULL,
                    ocr_engine=COALESCE(?,ocr_engine),ocr_language=COALESCE(?,ocr_language) WHERE id=?
            """, (now(), engine, language, document_id))
        self.kick()
        return True

    def refresh_layout(self, document_id):
        with self.ws.db() as db:
            scheduled = db.execute("""
                UPDATE bulk_jobs SET pending='layout',queued_at=?,canceled=0,error=NULL,finished=NULL
                WHERE id=? AND pending IS NULL
                  AND EXISTS (SELECT 1 FROM jobs WHERE document_id=? AND status='completed')
                  AND NOT EXISTS (SELECT 1 FROM jobs WHERE document_id=? AND status IN ('queued','running'))
            """, (now(), document_id, document_id, document_id)).rowcount
        if scheduled:
            self.kick()
        return bool(scheduled)

    @staticmethod
    def schedule_resume(db, document_id):
        return db.execute("""
            UPDATE bulk_jobs SET pending='resume',queued_at=?,canceled=0,error=NULL,finished=NULL
            WHERE id=? AND pending IS NULL
        """, (now(), document_id)).rowcount == 1

    def _claim(self):
        with self.ws.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM bulk_jobs WHERE active=1").fetchone():
                return None
            entry = db.execute("""
                SELECT k.*,b.owner,b.phase,COALESCE(k.ocr_engine,b.ocr_engine) AS engine,
                    COALESCE(k.ocr_language,b.ocr_language) AS language
                FROM bulk_jobs k JOIN bulk_batches b ON b.id=k.batch_id
                WHERE k.pending IS NOT NULL ORDER BY k.queued_at,k.rowid LIMIT 1
            """).fetchone()
            if not entry:
                return None
            document_id = entry["id"]
            db.execute("SAVEPOINT prepare_bulk_job")
            try:
                job, layout_from = self._prepare_job(db, entry)
            except (OSError, ValueError):
                LOGGER.exception("Could not prepare bulk job %s", document_id)
                db.execute("ROLLBACK TO prepare_bulk_job")
                db.execute("UPDATE bulk_jobs SET pending=NULL,error=?,finished=? WHERE id=?",
                           ("The saved inputs could not be prepared. Retry this folder.", now(), document_id))
                return ()
            finally:
                db.execute("RELEASE prepare_bulk_job")
            db.execute("UPDATE bulk_jobs SET active=1,pending=NULL WHERE id=?", (document_id,))
        return document_id, job, entry["phase"], entry["pending"] == "resume", layout_from

    def _prepare_job(self, db, entry):
        document_id = entry["id"]
        if entry["pending"] == "resume":
            job = db.execute("UPDATE jobs SET status='queued',finished=NULL,error=NULL WHERE id="
                             "(SELECT MAX(id) FROM jobs WHERE document_id=?) AND status='review' RETURNING id",
                             (document_id,)).fetchone()
            if not job:
                raise ValueError("The saved review is unavailable.")
            return job["id"], None
        if not db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
            files = {row["kind"]: row for row in db.execute("SELECT * FROM bulk_files WHERE job_id=? AND status='valid'",
                                                          (document_id,))}
            if set(files) != {"pdf", "pptx"}:
                raise ValueError("The saved pair is unavailable.")
            source, deck = self.ws.source(document_id), self.ws.paired_source(document_id)
            source.parent.mkdir(exist_ok=True)
            for kind, target in (("pdf", source), ("pptx", deck)):
                if not target.exists():
                    link(self.file_path(entry["batch_id"], files[kind]["id"]), target)
            path = f"bulk/{entry['batch_id']}/{entry['folder']}"
            db.execute("""
                INSERT INTO documents(id,filename,created,owner,phase,source_pptx,bulk_path) VALUES (?,?,?,?,?,?,?)
            """, (document_id, files["pdf"]["name"], now(), entry["owner"], entry["phase"], files["pptx"]["name"], path))
        db.execute("UPDATE documents SET approved=0 WHERE id=?", (document_id,))
        job = self.ws._create_job(db, document_id, entry["engine"], entry["language"])
        if job is None:
            raise ValueError("A processing attempt is already active.")
        if entry["pending"] == "layout":
            previous = db.execute("SELECT id,settings FROM jobs WHERE document_id=? AND status='completed' "
                                  "ORDER BY id DESC LIMIT 1", (document_id,)).fetchone()
            if not previous:
                raise ValueError("The saved layout attempt is unavailable.")
            db.execute("UPDATE jobs SET settings=? WHERE id=?", (previous["settings"], job))
            return job, previous["id"]
        return job, None

    def _release(self, document_id):
        with self.ws.db() as db:
            db.execute("UPDATE bulk_jobs SET active=0 WHERE id=?", (document_id,))

    def _finished(self, document_id):
        self._release(document_id)
        self.kick()

    def kick(self):
        while (entry := self._claim()) is not None:
            if not entry:
                continue
            document_id, job, phase, resume, layout_from = entry
            if self.ws._submit(document_id, job, layout_from, phase=phase, resume=resume,
                               on_done=lambda document_id=document_id: self._finished(document_id)):
                return
            self._release(document_id)

    def snapshot(self, batch_id, owner):
        with self.ws.db() as db:
            db.execute("BEGIN")
            batch = db.execute("SELECT id,phase,created,started FROM bulk_batches WHERE id=? AND owner=?",
                               (batch_id, owner)).fetchone()
            if not batch:
                return None
            files = self._files(db, batch_id)
            rows = list(db.execute("""
                SELECT k.*,j.status AS attempt_status,j.started,j.finished AS attempt_finished,
                    j.done,j.total,j.error AS attempt_error,d.filename AS source_pdf,d.bulk_path,
                    COALESCE(json_extract(j.result,'$.processing.stage'),json_extract(j.result,'$.stage')) AS stage,
                    COALESCE(json_extract(j.result,'$.processing.slide'),json_extract(j.result,'$.slide')) AS slide,
                    COALESCE(json_extract(j.result,'$.processing.total_slides'),
                        json_extract(j.result,'$.total_slides')) AS slides,
                    json_extract(j.result,'$.final_export.warnings') AS warnings,
                    json_extract(j.result,'$.final_export.error') AS export_error
                FROM bulk_jobs k LEFT JOIN documents d ON d.id=k.id
                LEFT JOIN jobs j ON j.id=(SELECT MAX(id) FROM jobs WHERE document_id=k.id)
                WHERE k.batch_id=? ORDER BY k.rowid
            """, (batch_id,)))
            exports = defaultdict(list)
            for row in db.execute("""
                SELECT j.document_id,j.status,j.error,
                    json_extract(j.result,'$.submission_export.output_folder') AS folder,
                    json_extract(j.result,'$.submission_export.outputs') AS outputs,
                    json_extract(j.result,'$.submission_export.xml_assets') AS assets,
                    json_extract(j.result,'$.submission_export.error') AS export_error
                FROM jobs j JOIN bulk_jobs k ON k.id=j.document_id
                WHERE k.batch_id=? AND j.status IN ('completed','failed')
                  AND json_extract(j.result,'$.submission_export.status') IN ('completed','partial')
                ORDER BY j.id DESC
            """, (batch_id,)):
                if row["status"] == "completed" or row["error"] == row["export_error"]:
                    exports[row["document_id"]].append(row)
        return dict(batch), rows, files, exports
