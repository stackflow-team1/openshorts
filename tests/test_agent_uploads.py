"""Agent uploads (POST/PUT /api/uploads -> process with upload_id) and the
per-job captions switch, through the MCP transport where the agent uses them."""
import asyncio
import os
import shutil
import subprocess

import httpx
import pytest

app_module = pytest.importorskip("app")


def _client():
    transport = httpx.ASGITransport(app=app_module.app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver",
                             headers={"X-Gemini-Key": "test-key"})


def _mcp(tool, arguments):
    async def _do():
        async with _client() as c:
            return await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": tool, "arguments": arguments}})
    return asyncio.run(_do())


def _tool_payload(resp):
    import json
    return json.loads(resp.json()["result"]["content"][0]["text"])


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    out_root = tmp_path / "output"; up_root = tmp_path / "uploads"
    out_root.mkdir(); up_root.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up_root))
    monkeypatch.setattr(app_module, "pending_uploads", {})
    # Never spawn main.py: capture the job instead.
    captured = {}
    async def fake_enqueue(*a, **k):
        captured["args"] = a; captured["kwargs"] = k
    monkeypatch.setattr(app_module, "_media_duration_seconds", lambda p: 120.0)
    return out_root, up_root, captured


def test_create_upload_is_an_mcp_tool():
    names = [t["name"] for t in __import__("mcp_server").TOOLS]
    assert "create_upload" in names
    pv = next(t for t in __import__("mcp_server").TOOLS if t["name"] == "process_video")
    assert "upload_id" in pv["inputSchema"]["properties"]
    assert "captions" in pv["inputSchema"]["properties"]
    assert "source_url" not in pv["inputSchema"]["required"]


def test_process_video_needs_a_source():
    payload = _tool_payload(_mcp("process_video", {"confirm_rights": True}))
    assert "source_url" in payload["error"] and "upload_id" in payload["error"]


def test_resumable_upload_routes_are_registered():
    paths = app_module.app.openapi()["paths"]
    assert "post" in paths["/api/uploads/init"]
    assert "get" in paths["/api/uploads/{upload_id}/status"]
    assert "put" in paths["/api/uploads/{upload_id}/chunks/{index}"]
    assert "post" in paths["/api/uploads/{upload_id}/complete"]


def test_put_then_process_with_upload_id_and_captions_off(dirs, monkeypatch):
    out_root, up_root, _ = dirs
    slot = _tool_payload(_mcp("create_upload", {"filename": "../talk.mp4"}))
    assert slot["upload_url"].endswith(f"/api/uploads/{slot['upload_id']}")
    assert "talk.mp4" in app_module.pending_uploads[slot["upload_id"]]["path"]
    assert ".." not in os.path.basename(app_module.pending_uploads[slot["upload_id"]]["path"])

    async def _put(body):
        async with _client() as c:
            return await c.put(f"/api/uploads/{slot['upload_id']}", content=body)
    assert asyncio.run(_put(b"")).status_code == 400
    resp = asyncio.run(_put(b"\x00" * 5000))
    assert resp.status_code == 200 and resp.json()["bytes"] == 5000

    # Intercept the job instead of running main.py.
    started = {}
    async def fake_put(item):
        started["job"] = item
    monkeypatch.setattr(app_module.job_queue, "put", fake_put)

    payload = _tool_payload(_mcp("process_video", {
        "upload_id": slot["upload_id"], "confirm_rights": True, "captions": False}))
    assert "job_id" in payload, payload
    job = app_module.jobs[payload["job_id"]]
    assert job["env"]["AUTO_CAPTIONS"] == "0"
    assert slot["upload_id"] not in app_module.pending_uploads
    input_path = job["cmd"][job["cmd"].index("-i") + 1]
    assert os.path.exists(input_path) and input_path.startswith(str(up_root))


def test_captions_default_leaves_env_alone(dirs, monkeypatch):
    slot = _tool_payload(_mcp("create_upload", {}))
    async def _put():
        async with _client() as c:
            return await c.put(f"/api/uploads/{slot['upload_id']}", content=b"x" * 100)
    assert asyncio.run(_put()).status_code == 200
    async def fake_put(item): pass
    monkeypatch.setattr(app_module.job_queue, "put", fake_put)
    payload = _tool_payload(_mcp("process_video", {"upload_id": slot["upload_id"], "confirm_rights": True}))
    env = app_module.jobs[payload["job_id"]]["env"]
    assert "AUTO_CAPTIONS" not in env
    assert env["AUTO_HOOK"] == "1"  # MCP default matches the dashboard


def test_unknown_or_unfinished_upload_id(dirs):
    async def _put():
        async with _client() as c:
            return await c.put("/api/uploads/nope", content=b"x")
    assert asyncio.run(_put()).status_code == 404
    slot = _tool_payload(_mcp("create_upload", {}))
    payload = _tool_payload(_mcp("process_video", {"upload_id": slot["upload_id"], "confirm_rights": True}))
    assert "PUT" in payload["error"]


def test_sweep_expires_old_slots(dirs, monkeypatch):
    slot = _tool_payload(_mcp("create_upload", {}))
    path = app_module.pending_uploads[slot["upload_id"]]["path"]
    open(path, "wb").write(b"x")
    assert slot["upload_id"] not in app_module._sweep_pending_uploads(now=__import__("time").time() + 1)
    gone = app_module._sweep_pending_uploads(now=__import__("time").time() + app_module.UPLOAD_TTL_SECONDS + 1)
    assert slot["upload_id"] in gone and not os.path.exists(path)


def test_delete_upload(dirs):
    slot = _tool_payload(_mcp("create_upload", {}))
    async def _del():
        async with _client() as c:
            return await c.delete(f"/api/uploads/{slot['upload_id']}")
    assert asyncio.run(_del()).status_code == 200
    assert slot["upload_id"] not in app_module.pending_uploads


def test_chunked_upload_resumes_validates_order_and_assembles_streamed_file(
        dirs, monkeypatch):
    _, up_root, _ = dirs
    monkeypatch.setattr(app_module, "UPLOAD_CHUNK_SIZE_BYTES", 4)
    monkeypatch.setattr(
        app_module, "_probe_uploaded_video",
        lambda path: {"duration_seconds": 120.0})

    async def _upload():
        async with _client() as c:
            init = await c.post("/api/uploads/init",
                                json={"filename": "../talk.mp4", "size": 10})
            assert init.status_code == 200
            session = init.json()
            upload_id = session["upload_id"]
            assert session["chunk_size"] == 4
            assert session["total_chunks"] == 3
            assert session["filename"] == "talk.mp4"
            assert session["status"] == "uploading"
            assert session["expires_in"] > 0

            out_of_order = await c.put(
                f"/api/uploads/{upload_id}/chunks/1", content=b"efgh")
            assert out_of_order.status_code == 409

            first = await c.put(
                f"/api/uploads/{upload_id}/chunks/0", content=b"abcd")
            assert first.status_code == 200
            retry = await c.put(
                f"/api/uploads/{upload_id}/chunks/0", content=b"abcd")
            assert retry.status_code == 200 and retry.json()["duplicate"]
            mismatch = await c.put(
                f"/api/uploads/{upload_id}/chunks/0", content=b"wxyz")
            assert mismatch.status_code == 409

            status = await c.get(f"/api/uploads/{upload_id}/status")
            assert status.json()["received_chunks"] == [0]
            assert status.json()["complete"] is False

            assert (await c.put(
                f"/api/uploads/{upload_id}/chunks/1", content=b"efgh")).status_code == 200
            assert (await c.put(
                f"/api/uploads/{upload_id}/chunks/2", content=b"ij")).status_code == 200
            completed = await c.post(f"/api/uploads/{upload_id}/complete")
            assert completed.status_code == 200
            assert completed.json()["complete"] is True
            assert completed.json()["status"] == "complete"
            assert completed.json()["duration_seconds"] == 120.0
            assembled = app_module.pending_uploads[upload_id]["path"]
            assert open(assembled, "rb").read() == b"abcdefghij"
            assert os.path.exists(assembled)

            # A session delete must not remove the assembled original.
            refused_delete = await c.delete(f"/api/uploads/{upload_id}")
            assert refused_delete.status_code == 409
            assert os.path.exists(assembled)
            return upload_id

    upload_id = asyncio.run(_upload())
    assert app_module.pending_uploads[upload_id]["bytes"] == 10
    async def fake_put(item):
        pass
    monkeypatch.setattr(app_module.job_queue, "put", fake_put)
    payload = _tool_payload(_mcp("process_video", {
        "upload_id": upload_id, "confirm_rights": True}))
    assert "job_id" in payload, payload
    job = app_module.jobs[payload["job_id"]]
    assert upload_id not in app_module.pending_uploads
    input_path = job["cmd"][job["cmd"].index("-i") + 1]
    assert open(input_path, "rb").read() == b"abcdefghij"


def test_chunked_upload_rejects_bad_extension_and_size_boundary(dirs, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILE_SIZE_MB", 1)

    async def _checks():
        async with _client() as c:
            bad_extension = await c.post(
                "/api/uploads/init", json={"filename": "payload.txt", "size": 10})
            assert bad_extension.status_code == 400
            fractional_size = await c.post(
                "/api/uploads/init", json={"filename": "fractional.mp4", "size": 1.5})
            assert fractional_size.status_code == 400
            over_limit = await c.post(
                "/api/uploads/init",
                json={"filename": "large.mp4", "size": 1024 * 1024 + 1})
            assert over_limit.status_code == 413
            accepted_boundary = await c.post(
                "/api/uploads/init",
                json={"filename": "max.mp4", "size": 1024 * 1024})
            assert accepted_boundary.status_code == 200
            assert app_module.UPLOAD_CHUNK_SIZE_BYTES == 8 * 1024 * 1024
            return accepted_boundary.json()["upload_id"]

    upload_id = asyncio.run(_checks())
    app_module.pending_uploads.pop(upload_id)


def test_chunk_session_expiry_removes_chunks_but_preserves_assembled_source(
        dirs, monkeypatch):
    _, up_root, _ = dirs
    monkeypatch.setattr(app_module, "UPLOAD_CHUNK_SIZE_BYTES", 4)
    monkeypatch.setattr(
        app_module, "_probe_uploaded_video",
        lambda path: {"duration_seconds": 120.0})

    async def _create_and_complete():
        async with _client() as c:
            incomplete = (await c.post(
                "/api/uploads/init", json={"filename": "unfinished.mp4", "size": 4}
            )).json()
            await c.put(
                f"/api/uploads/{incomplete['upload_id']}/chunks/0", content=b"data")
            incomplete_dir = app_module.pending_uploads[incomplete["upload_id"]]["chunk_dir"]

            complete = (await c.post(
                "/api/uploads/init", json={"filename": "source.mp4", "size": 4}
            )).json()
            await c.put(
                f"/api/uploads/{complete['upload_id']}/chunks/0", content=b"data")
            await c.post(f"/api/uploads/{complete['upload_id']}/complete")
            source_path = app_module.pending_uploads[complete["upload_id"]]["path"]
            return incomplete, incomplete_dir, complete, source_path

    incomplete, incomplete_dir, complete, source_path = asyncio.run(_create_and_complete())
    now = max(app_module.pending_uploads[incomplete["upload_id"]]["created"],
              app_module.pending_uploads[complete["upload_id"]]["created"])
    app_module._sweep_pending_uploads(now + app_module.UPLOAD_TTL_SECONDS + 1)
    assert not os.path.exists(incomplete_dir)
    assert not os.path.exists(os.path.join(up_root, ".chunks", complete["upload_id"]))
    assert os.path.exists(source_path)
    assert incomplete["upload_id"] not in app_module.pending_uploads
    assert complete["upload_id"] not in app_module.pending_uploads


def test_chunked_upload_respects_active_session_and_disk_limits(dirs, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_CONCURRENT_UPLOADS", 1)

    async def _checks():
        async with _client() as c:
            first = await c.post(
                "/api/uploads/init", json={"filename": "first.mp4", "size": 1024})
            assert first.status_code == 200
            blocked = await c.post(
                "/api/uploads/init", json={"filename": "second.mp4", "size": 1024})
            assert blocked.status_code == 429
            removed = await c.delete(f"/api/uploads/{first.json()['upload_id']}")
            assert removed.status_code == 200

            monkeypatch.setattr(
                app_module.shutil, "disk_usage",
                lambda _path: type("Usage", (), {"free": 0})())
            no_space = await c.post(
                "/api/uploads/init", json={"filename": "large.mp4", "size": 1024})
            assert no_space.status_code == 507

    asyncio.run(_checks())


def test_tmpfiles_link_is_refreshed_through_the_page():
    import file_hosts
    stale = "https://tmpfiles.org/dl/1787841143.abc/wAwNGnPW0lXW/video.mp4"
    fresh = "https://tmpfiles.org/dl/1787841582.def/wAwNGnPW0lXW/video.mp4"
    seen = {}
    def fetch(url):
        seen["page"] = url
        return f'<a href="{fresh}">Download</a>'
    assert file_hosts.resolve_tmpfiles(stale, fetch=fetch) == fresh
    assert seen["page"] == "https://tmpfiles.org/wAwNGnPW0lXW/video.mp4"
    assert file_hosts.resolve_tmpfiles("https://tmpfiles.org/wAwNGnPW0lXW/video.mp4", fetch=fetch) == fresh
    assert file_hosts.resolve_tmpfiles(stale, fetch=lambda u: "<html>no link</html>") == stale
    assert not file_hosts.is_tmpfiles("https://youtube.com/watch?v=x")


def test_auto_hook_reaches_the_job_env(dirs, monkeypatch):
    slot = _tool_payload(_mcp("create_upload", {}))
    async def _put():
        async with _client() as c:
            return await c.put(f"/api/uploads/{slot['upload_id']}", content=b"x" * 100)
    assert asyncio.run(_put()).status_code == 200
    async def fake_put(item): pass
    monkeypatch.setattr(app_module.job_queue, "put", fake_put)
    payload = _tool_payload(_mcp("process_video", {"upload_id": slot["upload_id"], "confirm_rights": True,
                                                   "auto_hook": True, "hook_style": "yellow"}))
    env = app_module.jobs[payload["job_id"]]["env"]
    assert env["AUTO_HOOK"] == "1" and env["AUTO_HOOK_STYLE"] == "yellow"


def test_auto_hook_false_opts_out(dirs, monkeypatch):
    slot = _tool_payload(_mcp("create_upload", {}))
    async def _put():
        async with _client() as c:
            return await c.put(f"/api/uploads/{slot['upload_id']}", content=b"x" * 100)
    assert asyncio.run(_put()).status_code == 200
    async def fake_put(item): pass
    monkeypatch.setattr(app_module.job_queue, "put", fake_put)
    payload = _tool_payload(_mcp("process_video", {"upload_id": slot["upload_id"], "confirm_rights": True, "auto_hook": False}))
    assert "AUTO_HOOK" not in app_module.jobs[payload["job_id"]]["env"]
