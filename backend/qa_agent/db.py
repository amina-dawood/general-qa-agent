from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import settings
from .utils import json_dumps, json_loads, utc_now


class Database:
    """Small SQLite repository optimized for a single-node local deployment.

    WAL mode allows dashboard reads while background generation/execution writes.
    The database stores JSON payloads for flexible domain models and summary columns
    for fast dashboard queries.
    """

    def __init__(self, path: Path = settings.database_path):
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS projects (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);

                    CREATE TABLE IF NOT EXISTS documents (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        status TEXT NOT NULL,
                        chunk_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id TEXT NOT NULL,
                        document_id TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        embedding BLOB,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                        FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);
                    CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id, chunk_index);

                    CREATE TABLE IF NOT EXISTS suites (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        feature TEXT NOT NULL,
                        status TEXT NOT NULL,
                        approved INTEGER NOT NULL DEFAULT 0,
                        test_count INTEGER NOT NULL DEFAULT 0,
                        requirement_count INTEGER NOT NULL DEFAULT 0,
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_suites_project ON suites(project_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        run_number INTEGER NOT NULL,
                        suite_id TEXT,
                        status TEXT NOT NULL,
                        passed_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        blocked_count INTEGER NOT NULL DEFAULT 0,
                        error_count INTEGER NOT NULL DEFAULT 0,
                        pass_rate REAL NOT NULL DEFAULT 0,
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        started_at TEXT NOT NULL,
                        ended_at TEXT NOT NULL DEFAULT '',
                        is_baseline INTEGER NOT NULL DEFAULT 0,
                        data_json TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_project_number ON runs(project_id, run_number);
                    CREATE INDEX IF NOT EXISTS idx_runs_project_started ON runs(project_id, started_at DESC);

                    CREATE TABLE IF NOT EXISTS project_counters (
                        project_id TEXT PRIMARY KEY,
                        next_run_number INTEGER NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS usage_ledger (
                        scope_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        requests INTEGER NOT NULL DEFAULT 0,
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        completion_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_tokens INTEGER NOT NULL DEFAULT 0,
                        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                        cost_usd REAL NOT NULL DEFAULT 0,
                        models_json TEXT NOT NULL DEFAULT '{}',
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_ledger(project_id);

                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress INTEGER NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT '',
                        result_json TEXT,
                        error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at DESC);
                    """
                )
                # Forward-compatible migration for databases created before v4.
                columns = {row["name"] for row in db.execute("PRAGMA table_info(usage_ledger)").fetchall()}
                if "reasoning_tokens" not in columns:
                    db.execute("ALTER TABLE usage_ledger ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0")
            self._initialized = True

    @contextmanager
    def transaction(self):
        self.initialize()
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        self.initialize()
        with self.connect() as db:
            db.execute(sql, tuple(params))
            db.commit()

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        self.initialize()
        with self.connect() as db:
            return db.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        self.initialize()
        with self.connect() as db:
            return list(db.execute(sql, tuple(params)).fetchall())

    # Projects -----------------------------------------------------------------
    def save_project(self, project: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now()
        project.setdefault("created_at", now)
        project["updated_at"] = now
        self.execute(
            """INSERT INTO projects(id,name,slug,status,data_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,slug=excluded.slug,status=excluded.status,
               data_json=excluded.data_json,updated_at=excluded.updated_at""",
            (project["id"], project["name"], project["slug"], project.get("status", "active"), json_dumps(project), project["created_at"], project["updated_at"]),
        )
        return project

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT data_json FROM projects WHERE id=?", (project_id,))
        return json_loads(row["data_json"], {}) if row else None

    def list_projects(self) -> List[Dict[str, Any]]:
        return [json_loads(row["data_json"], {}) for row in self.all("SELECT data_json FROM projects ORDER BY updated_at DESC")]

    def document_counts(self) -> Dict[str, int]:
        rows = self.all("SELECT project_id, COUNT(*) AS count FROM documents GROUP BY project_id")
        return {str(row["project_id"]): int(row["count"]) for row in rows}

    # Documents ----------------------------------------------------------------
    def save_document(self, doc: Dict[str, Any]) -> None:
        # Use UPSERT rather than INSERT OR REPLACE. SQLite REPLACE deletes the
        # old row before inserting the replacement, which can trigger the
        # chunks(document_id) ON DELETE CASCADE and silently remove embeddings.
        self.execute(
            """INSERT INTO documents(id,project_id,name,path,checksum,status,chunk_count,created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 project_id=excluded.project_id,
                 name=excluded.name,
                 path=excluded.path,
                 checksum=excluded.checksum,
                 status=excluded.status,
                 chunk_count=excluded.chunk_count,
                 created_at=excluded.created_at""",
            (
                doc["id"],
                doc["project_id"],
                doc["name"],
                doc["path"],
                doc["checksum"],
                doc.get("status", "uploaded"),
                int(doc.get("chunk_count", 0)),
                doc["created_at"],
            ),
        )

    def update_document_path(self, doc_id: str, path: str) -> None:
        self.execute("UPDATE documents SET path=? WHERE id=?", (path, doc_id))

    def update_document_status(self, doc_id: str, status: str, chunk_count: int = 0) -> None:
        self.execute("UPDATE documents SET status=?, chunk_count=? WHERE id=?", (status, chunk_count, doc_id))

    def list_documents(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self.all("SELECT * FROM documents WHERE project_id=? ORDER BY created_at DESC", (project_id,))
        return [dict(row) for row in rows]

    def find_document_by_checksum(self, project_id: str, checksum: str) -> Optional[Dict[str, Any]]:
        row = self.one(
            "SELECT * FROM documents WHERE project_id=? AND checksum=? ORDER BY created_at DESC LIMIT 1",
            (project_id, checksum),
        )
        return dict(row) if row else None

    def replace_chunks(self, project_id: str, document_id: str, chunks: List[Dict[str, Any]]) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            db.executemany(
                "INSERT INTO chunks(project_id,document_id,chunk_index,text,embedding,metadata_json) VALUES(?,?,?,?,?,?)",
                [
                    (project_id, document_id, item["chunk_index"], item["text"], item.get("embedding"), json_dumps(item.get("metadata", {})))
                    for item in chunks
                ],
            )

    def project_chunks(self, project_id: str) -> List[sqlite3.Row]:
        return self.all(
            "SELECT id,document_id,chunk_index,text,embedding,metadata_json FROM chunks WHERE project_id=? ORDER BY id",
            (project_id,),
        )

    # Suites -------------------------------------------------------------------
    def save_suite(self, suite: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now()
        suite.setdefault("created_at", now)
        suite["updated_at"] = now
        self.execute(
            """INSERT INTO suites(id,project_id,version,name,feature,status,approved,test_count,requirement_count,data_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,approved=excluded.approved,test_count=excluded.test_count,
               requirement_count=excluded.requirement_count,data_json=excluded.data_json,updated_at=excluded.updated_at""",
            (suite["id"], suite["project_id"], int(suite["version"]), suite["name"], suite["feature"], suite.get("status", "draft"), 1 if suite.get("approved") else 0, len(suite.get("test_cases", [])), len(suite.get("requirements", [])), json_dumps(suite), suite["created_at"], suite["updated_at"]),
        )
        usage = suite.get("generation_ai_usage") or {}
        if usage:
            self.save_usage(suite["id"], suite["project_id"], "suite_generation", usage)
        return suite

    def get_suite(self, suite_id: str) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT data_json FROM suites WHERE id=?", (suite_id,))
        return json_loads(row["data_json"], {}) if row else None

    def list_suites(self, project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.all("SELECT data_json FROM suites WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))
        return [json_loads(row["data_json"], {}) for row in rows]

    # Runs ---------------------------------------------------------------------
    def next_run_number(self, project_id: str) -> int:
        """Atomically reserve the next friendly run number for a project."""
        with self.transaction() as db:
            counter = db.execute(
                "SELECT next_run_number FROM project_counters WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if counter:
                number = int(counter["next_run_number"])
                db.execute(
                    "UPDATE project_counters SET next_run_number=? WHERE project_id=?",
                    (number + 1, project_id),
                )
                return number

            row = db.execute(
                "SELECT COALESCE(MAX(run_number),0)+1 AS next_number FROM runs WHERE project_id=?",
                (project_id,),
            ).fetchone()
            number = int(row["next_number"])
            db.execute(
                "INSERT INTO project_counters(project_id,next_run_number) VALUES(?,?)",
                (project_id, number + 1),
            )
            return number

    def save_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        self.execute(
            """INSERT INTO runs(id,project_id,run_number,suite_id,status,passed_count,failed_count,blocked_count,error_count,pass_rate,duration_ms,started_at,ended_at,is_baseline,data_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,passed_count=excluded.passed_count,failed_count=excluded.failed_count,
               blocked_count=excluded.blocked_count,error_count=excluded.error_count,pass_rate=excluded.pass_rate,duration_ms=excluded.duration_ms,
               ended_at=excluded.ended_at,is_baseline=excluded.is_baseline,data_json=excluded.data_json""",
            (run["id"], run["project_id"], int(run["run_number"]), run.get("suite_id"), run.get("status", "running"), int(run.get("passed_count", 0)), int(run.get("failed_count", 0)), int(run.get("blocked_count", 0)), int(run.get("error_count", 0)), float(run.get("pass_rate", 0)), int(run.get("duration_ms", 0)), run.get("started_at", utc_now()), run.get("ended_at", ""), 1 if run.get("is_baseline") else 0, json_dumps(run)),
        )
        self.save_usage(run["id"], run["project_id"], "test_run", run.get("ai_usage") or {})
        return run

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT data_json FROM runs WHERE id=?", (run_id,))
        return json_loads(row["data_json"], {}) if row else None

    def list_runs(self, project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.all("SELECT data_json FROM runs WHERE project_id=? ORDER BY run_number DESC LIMIT ?", (project_id, limit))
        return [json_loads(row["data_json"], {}) for row in rows]

    def count_runs(self, project_id: str) -> int:
        row = self.one("SELECT COUNT(*) AS count FROM runs WHERE project_id=?", (project_id,))
        return int(row["count"] if row else 0)

    def list_run_summaries(self, project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.all(
            """SELECT r.id,r.project_id,r.run_number,r.suite_id,r.status,r.passed_count,r.failed_count,
                      r.blocked_count,r.error_count,r.pass_rate,r.duration_ms,r.started_at,r.ended_at,r.is_baseline,
                      COALESCE(s.name,'') AS suite_name
               FROM runs r LEFT JOIN suites s ON s.id=r.suite_id
               WHERE r.project_id=? ORDER BY r.run_number DESC LIMIT ?""",
            (project_id, limit),
        )
        return [
            {
                **dict(row),
                "display_id": f"Run {int(row['run_number'])}",
                "is_baseline": bool(row["is_baseline"]),
                "results": [],
                "ai_usage": {},
            }
            for row in rows
        ]

    def set_baseline(self, project_id: str, run_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE runs SET is_baseline=0 WHERE project_id=?", (project_id,))
            db.execute("UPDATE runs SET is_baseline=1 WHERE id=? AND project_id=?", (run_id, project_id))
            rows = db.execute("SELECT id,data_json,is_baseline FROM runs WHERE project_id=?", (project_id,)).fetchall()
            for row in rows:
                data = json_loads(row["data_json"], {})
                data["is_baseline"] = bool(row["id"] == run_id)
                db.execute("UPDATE runs SET data_json=? WHERE id=?", (json_dumps(data), row["id"]))

    def delete_run(self, run_id: str) -> bool:
        """Delete one persisted run and its run-scoped AI usage.

        Friendly run numbers are intentionally never renumbered after deletion;
        gaps preserve historical identity and avoid changing references in demos/reports.
        """
        with self.transaction() as db:
            row = db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM usage_ledger WHERE scope_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            return True

    # AI usage -----------------------------------------------------------------
    def save_usage(self, scope_id: str, project_id: str, kind: str, usage: Dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO usage_ledger(scope_id,project_id,kind,requests,prompt_tokens,completion_tokens,total_tokens,cached_tokens,reasoning_tokens,cost_usd,models_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scope_id) DO UPDATE SET requests=excluded.requests,prompt_tokens=excluded.prompt_tokens,
               completion_tokens=excluded.completion_tokens,total_tokens=excluded.total_tokens,cached_tokens=excluded.cached_tokens,
               reasoning_tokens=excluded.reasoning_tokens,cost_usd=excluded.cost_usd,models_json=excluded.models_json,updated_at=excluded.updated_at""",
            (
                scope_id, project_id, kind, int(usage.get("requests", 0) or 0), int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0), int(usage.get("total_tokens", 0) or 0),
                int(usage.get("cached_tokens", 0) or 0), int(usage.get("reasoning_tokens", 0) or 0),
                float(usage.get("cost_usd", 0.0) or 0.0), json_dumps(usage.get("models", {}) or {}), utc_now(),
            ),
        )

    def aggregate_usage(self, project_id: str) -> Dict[str, Any]:
        row = self.one(
            """SELECT COALESCE(SUM(requests),0) AS requests,COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                      COALESCE(SUM(completion_tokens),0) AS completion_tokens,COALESCE(SUM(total_tokens),0) AS total_tokens,
                      COALESCE(SUM(cached_tokens),0) AS cached_tokens,COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                      COALESCE(SUM(cost_usd),0) AS cost_usd
               FROM usage_ledger WHERE project_id=?""",
            (project_id,),
        )
        models: Dict[str, int] = {}
        for item in self.all("SELECT models_json FROM usage_ledger WHERE project_id=?", (project_id,)):
            for model, count in (json_loads(item["models_json"], {}) or {}).items():
                models[str(model)] = models.get(str(model), 0) + int(count or 0)
        return {
            "requests": int(row["requests"] if row else 0),
            "prompt_tokens": int(row["prompt_tokens"] if row else 0),
            "completion_tokens": int(row["completion_tokens"] if row else 0),
            "total_tokens": int(row["total_tokens"] if row else 0),
            "cached_tokens": int(row["cached_tokens"] if row else 0),
            "reasoning_tokens": int(row["reasoning_tokens"] if row else 0),
            "cost_usd": round(float(row["cost_usd"] if row else 0.0), 8),
            "models": models,
        }

    # Jobs ---------------------------------------------------------------------
    def save_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        self.execute(
            """INSERT INTO jobs(id,project_id,kind,status,progress,message,result_json,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,progress=excluded.progress,message=excluded.message,
               result_json=excluded.result_json,error=excluded.error,updated_at=excluded.updated_at""",
            (job["id"], job.get("project_id"), job["kind"], job["status"], int(job.get("progress", 0)), job.get("message", ""), json_dumps(job.get("result")) if job.get("result") is not None else None, job.get("error", ""), job["created_at"], job["updated_at"]),
        )
        return job

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            return None
        return {
            "id": row["id"], "project_id": row["project_id"], "kind": row["kind"], "status": row["status"],
            "progress": row["progress"], "message": row["message"], "result": json_loads(row["result_json"]),
            "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def fail_incomplete_jobs(self) -> None:
        """Mark work interrupted by a process restart instead of leaving stale spinners."""
        now = utc_now()
        self.execute(
            """UPDATE jobs SET status='failed', progress=100, message='Interrupted',
                      error='The application restarted before this job completed.', updated_at=?
               WHERE status IN ('queued','running')""",
            (now,),
        )


database = Database()
database.initialize()
