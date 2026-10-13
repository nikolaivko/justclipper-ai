import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.main import app
from app.services.file_service import FileService
from app.services.job_store import job_store
from app.services.storage_lifecycle import InsufficientStorage, storage_lifecycle


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    jobs_dir = tmp_path / "jobs"
    input_dir.mkdir()
    jobs_dir.mkdir()
    monkeypatch.setattr(config, "INPUT_DIR", input_dir)
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    monkeypatch.setattr(storage_lifecycle, "root", tmp_path)
    sample = tmp_path / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=10",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "0.4", "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
        str(sample),
    ], check=True, capture_output=True)
    return input_dir, sample


def test_browser_stream_upload_writes_one_copy(upload_env, monkeypatch):
    input_dir, sample = upload_env
    import starlette.formparsers as formparsers

    def no_multipart_spool(*_args, **_kwargs):
        raise AssertionError("multipart disk spool was used")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", no_multipart_spool)
    calls = []
    monkeypatch.setattr(storage_lifecycle, "prepare_for_new_video", lambda: calls.append("cleanup"))
    body = sample.read_bytes()
    response = TestClient(app).post("/api/upload/stream", content=body, headers={
        "Content-Type": "application/octet-stream",
        "X-Upload-Filename": "sample.mp4",
        "X-Upload-Size": str(len(body)),
    })
    assert response.status_code == 200
    saved = job_store.get(response.json()["job_id"])
    assert saved["source_cleanup_eligible"] is True
    assert saved["storage_profile"] == "DIRECT_UPLOAD"
    assert Path(saved["filepath"]).read_bytes() == body
    assert len(list(input_dir.iterdir())) == 1
    assert calls == ["cleanup"]


def test_stream_upload_fails_preflight_without_partial_file(upload_env, monkeypatch):
    input_dir, sample = upload_env
    monkeypatch.setattr(storage_lifecycle, "prepare_for_new_video", lambda: None)
    monkeypatch.setattr(storage_lifecycle, "require_upload_space",
                        lambda _size: (_ for _ in ()).throw(
                            InsufficientStorage("INSUFFICIENT_STORAGE")))
    body = sample.read_bytes()
    response = TestClient(app).post("/api/upload/stream", content=body, headers={
        "X-Upload-Filename": "sample.mp4", "X-Upload-Size": str(len(body))})
    assert response.status_code == 507
    assert not list(input_dir.iterdir())


def test_incomplete_stream_deletes_partial_file(upload_env):
    input_dir, _ = upload_env

    async def chunks():
        yield b"abc"

    with pytest.raises(ValueError, match="UPLOAD_SIZE_MISMATCH"):
        asyncio.run(FileService.save_stream(chunks(), "deadbeef", "partial.mp4", 5))
    assert not list(input_dir.iterdir())


def test_size_header_must_match_raw_body(upload_env):
    input_dir, sample = upload_env
    body = sample.read_bytes()
    response = TestClient(app).post("/api/upload/stream", content=body, headers={
        "X-Upload-Filename": "sample.mp4", "X-Upload-Size": str(len(body) + 1)})
    assert response.status_code == 400
    assert not list(input_dir.iterdir())
