import job_store


def test_job_progress_survives_database_reopen(tmp_path):
    database = str(tmp_path / "output" / ".openshorts-jobs.sqlite3")
    job_store.create(database, {
        "job_id": "job-1",
        "status": "processing",
        "source_video_path": "/workspace/uploads/source.mp4",
        "source_metadata": {"size_bytes": 1024},
    })
    job_store.stage(database, "job-1", "transcription", "completed")
    job_store.update(database, "job-1", selected_clip_timestamps=[
        {"start": 10, "end": 30}, {"start": 40, "end": 60},
    ])
    job_store.clip(database, "job-1", 0, "completed", filename="clip_1.mp4")
    job_store.clip(database, "job-1", 1, "processing")

    record = job_store.get(database, "job-1")

    assert record["status"] == "processing"
    assert record["source_metadata"]["size_bytes"] == 1024
    assert record["completed_stages"] == ["transcription"]
    assert record["selected_clip_timestamps"][1] == {"start": 40, "end": 60}
    assert record["clips"]["0"]["path"] == "clip_1.mp4"
    assert record["clips"]["1"]["status"] == "processing"
    assert record["generated_clip_paths"] == ["clip_1.mp4"]


def test_delete_owner_removes_only_their_records(tmp_path):
    database = str(tmp_path / "jobs.sqlite3")
    job_store.create(database, {"job_id": "owned", "owner_id": "user-1"})
    job_store.create(database, {"job_id": "other", "owner_id": "user-2"})

    assert job_store.delete_owner(database, "user-1") == 1
    assert job_store.get(database, "owned") is None
    assert job_store.get(database, "other")["owner_id"] == "user-2"
