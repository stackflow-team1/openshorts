"""Durable job progress records stored alongside workspace-backed outputs."""
import json
import os
import sqlite3
import time
from contextlib import closing


def _connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            current_stage TEXT,
            updated_at REAL NOT NULL,
            record_json TEXT NOT NULL
        )
    """)
    return db


def create(path, record):
    now = time.time()
    value = {
        "job_id": record["job_id"],
        "status": record.get("status", "queued"),
        "current_stage": record.get("current_stage", "queued"),
        "created_at": record.get("created_at", now),
        "updated_at": now,
        "completed_stages": [],
        "failed_stages": [],
        "selected_clip_timestamps": [],
        "gemini_results": None,
        "generated_clip_paths": [],
        "clips": {},
        "interrupted": False,
        **record,
    }
    with closing(_connect(path)) as db, db:
        db.execute(
            "INSERT INTO jobs(job_id, status, current_stage, updated_at, record_json) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "status=excluded.status, current_stage=excluded.current_stage, "
            "updated_at=excluded.updated_at, record_json=excluded.record_json",
            (value["job_id"], value["status"], value["current_stage"], now,
             json.dumps(value, separators=(",", ":"))),
        )
    return value


def get(path, job_id):
    with closing(_connect(path)) as db:
        row = db.execute("SELECT record_json FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return json.loads(row["record_json"]) if row else None


def _update_in_transaction(db, job_id, changes):
    row = db.execute("SELECT record_json FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row:
        value = json.loads(row["record_json"])
    else:
        value = {"job_id": job_id, "created_at": time.time(),
                 "completed_stages": [], "failed_stages": [],
                 "selected_clip_timestamps": [], "generated_clip_paths": [],
                 "clips": {}, "interrupted": False}
    value.update(changes)
    value["updated_at"] = time.time()
    db.execute(
        "INSERT INTO jobs(job_id, status, current_stage, updated_at, record_json) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
        "status=excluded.status, current_stage=excluded.current_stage, "
        "updated_at=excluded.updated_at, record_json=excluded.record_json",
        (job_id, value.get("status", "queued"), value.get("current_stage"),
         value["updated_at"], json.dumps(value, separators=(",", ":"))),
    )
    return value


def update(path, job_id, **changes):
    with closing(_connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return _update_in_transaction(db, job_id, changes)


def stage(path, job_id, name, state, error=None):
    with closing(_connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT record_json FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        record = json.loads(row["record_json"]) if row else {}
        completed = set(record.get("completed_stages") or [])
        failed = set(record.get("failed_stages") or [])
        changes = {"current_stage": name, "stage_updated_at": time.time()}
        if state == "completed":
            completed.add(name)
            failed.discard(name)
            changes.update(completed_stages=sorted(completed), failed_stages=sorted(failed))
        elif state == "failed":
            failed.add(name)
            changes.update(failed_stages=sorted(failed), error=error)
        else:
            changes["stage_started_at"] = time.time()
        return _update_in_transaction(db, job_id, changes)


def clip(path, job_id, index, state, filename=None, error=None):
    with closing(_connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT record_json FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        record = json.loads(row["record_json"]) if row else {}
        clips = dict(record.get("clips") or {})
        clips[str(index)] = {
            "status": state,
            "path": filename,
            "error": error,
            "updated_at": time.time(),
        }
        generated = set(record.get("generated_clip_paths") or [])
        if state == "completed" and filename:
            generated.add(filename)
        return _update_in_transaction(
            db, job_id, {"clips": clips, "generated_clip_paths": sorted(generated)})


def list_jobs(path, limit=50):
    with _connect(path) as db:
        rows = db.execute(
            "SELECT record_json FROM jobs ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [json.loads(row["record_json"]) for row in rows]


def delete_owner(path, owner_id):
    owner_id = str(owner_id)
    with closing(_connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT job_id, record_json FROM jobs").fetchall()
        job_ids = [
            row["job_id"] for row in rows
            if str(json.loads(row["record_json"]).get("owner_id")) == owner_id
        ]
        db.executemany("DELETE FROM jobs WHERE job_id = ?",
                       ((job_id,) for job_id in job_ids))
    return len(job_ids)
