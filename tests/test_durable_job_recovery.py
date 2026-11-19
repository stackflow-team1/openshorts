import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

import job_store

app_module = pytest.importorskip("app")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "job_queue", asyncio.PriorityQueue())
    return output


def test_restart_requeues_job_without_discarding_checkpoints(workspace):
    job_id = str(uuid.uuid4())
    job_dir = workspace / job_id
    job_dir.mkdir()
    source = workspace / "source.mp4"
    source.write_bytes(b"source")
    database = app_module._job_store_path()
    job_store.create(database, {
        "job_id": job_id,
        "status": "processing",
        "source_video_path": str(source),
    })
    job_store.stage(database, job_id, "transcription", "completed")
    job_store.update(database, job_id, selected_clip_timestamps=[
        {"start": 10, "end": 30}, {"start": 40, "end": 60},
    ])
    job_store.clip(database, job_id, 0, "completed", filename="clip_1.mp4")
    (job_dir / app_module._RESUME_FILE).write_text(json.dumps({
        "cmd": ["python", "main.py", "-i", str(source), "-o", str(job_dir)],
        "priority": 2,
        "user_id": None,
        "reservation_id": None,
        "watermark": False,
        "attempts": 0,
    }))

    app_module._resume_interrupted_jobs()

    record = job_store.get(database, job_id)
    assert app_module.jobs[job_id]["status"] == "queued"
    assert record["status"] == "queued"
    assert record["interrupted"] is True
    assert record["completed_stages"] == ["transcription"]
    assert record["clips"]["0"]["status"] == "completed"
    assert record["selected_clip_timestamps"][1] == {"start": 40, "end": 60}
    assert app_module._job_progress(record)["completed_clips"] == 1
    assert app_module.job_queue.qsize() == 1


def test_missing_resume_manifest_marks_job_retryable(workspace):
    job_id = str(uuid.uuid4())
    source = workspace / "source.mp4"
    source.write_bytes(b"source")
    database = app_module._job_store_path()
    job_store.create(database, {
        "job_id": job_id,
        "status": "processing",
        "current_stage": "transcription",
        "source_video_path": str(source),
    })

    app_module._reconcile_persistent_job_records()

    record = job_store.get(database, job_id)
    assert record["status"] == "failed"
    assert record["interrupted"] is True
    assert record["failed_stages"] == ["transcription"]
    assert app_module._job_progress(record)["retryable"] is True


def test_status_and_history_expose_durable_progress_without_internal_paths(workspace):
    job_id = str(uuid.uuid4())
    source = workspace / "source.mp4"
    source.write_bytes(b"source")
    job_store.create(app_module._job_store_path(), {
        "job_id": job_id,
        "status": "processing",
        "current_stage": "transcription",
        "source_video_path": str(source),
        "source_metadata": {"name": source.name, "size_bytes": source.stat().st_size},
        "selected_clip_timestamps": [{"start": 10, "end": 30}],
    })
    app_module.jobs[job_id] = {
        "status": "processing",
        "logs": app_module._TimedLog(["Transcribing"]),
        "output_dir": str(workspace / job_id),
        "user_id": None,
        "result": None,
        "partial": None,
    }

    async def get_responses():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app),
            base_url="http://test",
        ) as client:
            return (
                await client.get(f"/api/status/{job_id}"),
                await client.get("/api/jobs"),
            )

    status, history = asyncio.run(get_responses())
    assert status.status_code == 200
    assert status.json()["progress"]["current_stage"] == "transcription"
    assert history.status_code == 200
    assert history.json()["jobs"][0]["job_id"] == job_id
    assert str(source) not in history.text


def test_clip_stages_do_not_overstate_progress_for_pending_clips():
    record = {
        "status": "processing",
        "completed_stages": [
            "upload", "metadata_extraction", "audio_extraction", "transcription",
            "gemini_scoring", "gemini_detailed_analysis", "clip_selection",
            "scene_detection", "clip_rendering", "hook_overlays",
            "caption_generation", "final_mp4_generation",
        ],
        "selected_clip_timestamps": [{"start": 10, "end": 30}, {"start": 40, "end": 60}],
        "clips": {"0": {"status": "completed"}},
    }

    assert app_module._job_progress(record)["percent"] == 79


def test_fresh_backend_process_recovers_saved_job(workspace):
    job_id = str(uuid.uuid4())
    job_dir = workspace / job_id
    job_dir.mkdir()
    source = workspace / "source.mp4"
    source.write_bytes(b"source")
    database = app_module._job_store_path()
    job_store.create(database, {
        "job_id": job_id,
        "status": "processing",
        "current_stage": "clip_rendering",
        "source_video_path": str(source),
    })
    job_store.stage(database, job_id, "transcription", "completed")
    job_store.update(database, job_id, selected_clip_timestamps=[
        {"start": 10, "end": 30}, {"start": 40, "end": 60},
    ])
    job_store.clip(database, job_id, 0, "completed", filename="clip_1.mp4")
    (job_dir / app_module._RESUME_FILE).write_text(json.dumps({
        "cmd": ["python", "main.py", "-i", str(source), "-o", str(job_dir)],
        "priority": 2,
        "user_id": None,
        "reservation_id": None,
        "watermark": False,
        "attempts": 0,
    }))

    script = """
import asyncio, sys
import app, job_store
job_id = sys.argv[1]
async def no_worker():
    await asyncio.Event().wait()
app.process_queue = no_worker
async def verify():
    async with app.lifespan(app.app):
        record = job_store.get(app._job_store_path(), job_id)
        assert app.jobs[job_id]['status'] == 'queued'
        assert record['interrupted'] is True
        assert record['completed_stages'] == ['transcription']
        assert record['clips']['0']['status'] == 'completed'
        assert app._job_progress(record)['pending_clips'] == 1
        print('fresh process recovered the persisted job')
asyncio.run(verify())
"""
    env = os.environ.copy()
    env["BILLING_ENABLED"] = "0"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c", script, job_id],
        cwd=workspace.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "fresh process recovered the persisted job" in result.stdout
