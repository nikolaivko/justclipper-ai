import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.main import app
from app.services.job_store import JobStore, job_store
from app.services.render_resolution import RenderResolutionError, resolve_render
from app.services.youtube_publisher import youtube_publisher
from app.services.storage_lifecycle import storage_lifecycle


@pytest.fixture
def completed_job(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    jobs_dir = tmp_path / "jobs"
    output_dir.mkdir()
    jobs_dir.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "DATA_JOBS", jobs_dir)
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)

    renders = []
    for rank in (1, 2):
        path = output_dir / f"firstjob_clip_{rank:02d}_Clip_{rank}.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"color=c={'red' if rank == 1 else 'blue'}:s=32x32:r=10",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.4", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path)],
            check=True, capture_output=True,
        )
        renders.append({"rank": rank, "title": f"Clip {rank}", "success": True,
                        "output_path": str(path)})
    job_store.set("firstjob", {"id": "firstjob", "status": "COMPLETED",
                               "renders": renders})
    yield TestClient(app), renders, output_dir


def test_each_render_resolves_its_own_mp4_and_persists(completed_job, monkeypatch):
    client, renders, _ = completed_job
    upload = Mock(return_value={"status": "UPLOADED", "video_id": "youtube2",
                                "published_at": 123.0})
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)

    for rank, render in enumerate(renders, 1):
        media = client.get(f"/api/jobs/firstjob/renders/{rank}/media")
        assert media.status_code == 200
        assert media.content == Path(render["output_path"]).read_bytes()

    response = client.post("/api/youtube/publish/firstjob/2")
    assert response.status_code == 200
    assert response.json()["video_id"] == "youtube2"
    assert upload.call_args.kwargs["file_path"] == Path(renders[1]["output_path"])
    assert "privacy_status" not in upload.call_args.kwargs
    assert upload.call_args.kwargs["title"] != "Clip 2 - Highlights"

    persisted = JobStore().get("firstjob")
    assert persisted["renders"][0].get("publish_status") is None
    assert persisted["renders"][1]["publish_status"] == "UPLOADED"
    assert persisted["renders"][1]["youtube_video_id"] == "youtube2"
    assert persisted["renders"][1]["published_at"] == 123.0
    assert persisted["renders"][1]["youtube_metadata"]["title"] == upload.call_args.kwargs["title"]

    again = client.post("/api/youtube/publish/firstjob/2")
    assert again.status_code == 200
    assert again.json()["video_id"] == "youtube2"
    assert upload.call_count == 1

    upload.return_value = {"status": "UPLOADED", "video_id": "youtube1"}
    assert client.post("/api/youtube/publish/firstjob/1").status_code == 200
    assert upload.call_args.kwargs["file_path"] == Path(renders[0]["output_path"])
    assert JobStore().get("firstjob")["renders"][1]["youtube_video_id"] == "youtube2"


def test_reviewed_title_is_used_for_upload_and_uploaded_title_is_locked(
        completed_job, monkeypatch):
    client, _, _ = completed_job
    upload = Mock(return_value={"status": "UPLOADED", "video_id": "reviewed1234",
                                "published_at": 123.0})
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    assert client.patch("/api/jobs/firstjob/renders/1/title",
                        json={"title": "  A real  gym  moment  "}).json()["title"] == \
        "A real gym moment"
    assert client.patch("/api/jobs/firstjob/renders/1/title",
                        json={"title": "<bad>"}).status_code == 422
    response = client.post("/api/youtube/publish/firstjob/1")
    assert response.status_code == 200
    assert upload.call_args.kwargs["title"] == "A real gym moment"
    assert client.patch("/api/jobs/firstjob/renders/1/title",
                        json={"title": "Changed later"}).status_code == 409

def test_partial_render_job_can_publish_ready_clip_without_losing_source(
        completed_job, monkeypatch, tmp_path):
    client, renders, _ = completed_job
    source = tmp_path / "input" / "firstjob.mp4"
    source.parent.mkdir()
    source.write_bytes(b"source retained for unfinished render")
    job = job_store.get("firstjob")
    job.update(status="FAILED", error="INCOMPLETE_RENDER_COUNT",
               filepath=str(source), lifecycle_version=1,
               source_cleanup_eligible=True,
               render_request={"candidates": [{"rank": 1}, {"rank": 2}]})
    job["renders"][1]["success"] = False
    job_store.set("firstjob", job)
    monkeypatch.setattr(storage_lifecycle, "root", tmp_path)
    upload = Mock(return_value={"status": "UPLOADED", "video_id": "partial123",
                                "published_at": 123.0})
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)

    response = client.post("/api/youtube/publish-all/firstjob")
    assert response.status_code == 200
    assert [(item["rank"], item["status"]) for item in response.json()["results"]] == [
        (1, "UPLOADED"), (2, "SKIPPED")]
    assert upload.call_count == 1
    assert source.exists()
    saved = job_store.get("firstjob")
    assert saved["status"] == "FAILED"
    assert saved["renders"][0]["publish_status"] == "UPLOADED"
    assert saved["renders"][1]["success"] is False


def test_unrelated_failed_job_still_cannot_publish(completed_job, monkeypatch):
    client, _, _ = completed_job
    job = job_store.get("firstjob")
    job.update(status="FAILED", error="ANALYSIS_FAILED")
    job_store.set("firstjob", job)
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    response = client.post("/api/youtube/publish/firstjob/1")
    assert response.status_code == 409
    assert response.json()["detail"] == "JOB_NOT_COMPLETED"
    upload.assert_not_called()


@pytest.mark.parametrize("url,detail", [
    ("/api/youtube/publish/missing/1", "JOB_NOT_FOUND"),
    ("/api/youtube/publish/firstjob/3", "RENDER_NOT_FOUND"),
    ("/api/youtube/publish/otherjob/2", "RENDER_NOT_FOUND"),
])
def test_wrong_job_or_rank_cannot_upload(completed_job, monkeypatch, url, detail):
    client, renders, _ = completed_job
    job_store.set("otherjob", {"id": "otherjob", "status": "COMPLETED",
                               "renders": [{**renders[0], "rank": 1}]})
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    response = client.post(url)
    assert response.status_code == 404
    assert response.json()["detail"] == detail
    upload.assert_not_called()


def test_unfinished_missing_invalid_and_outside_output(completed_job, monkeypatch, tmp_path):
    client, renders, output_dir = completed_job
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    job = job_store.get("firstjob")
    job["renders"][1]["success"] = False
    job_store.set("firstjob", job)
    assert client.post("/api/youtube/publish/firstjob/2").json()["detail"] == "RENDER_NOT_COMPLETED"

    job["renders"][1]["success"] = True
    Path(renders[1]["output_path"]).unlink()
    job_store.set("firstjob", job)
    assert client.post("/api/youtube/publish/firstjob/2").json()["detail"] == "OUTPUT_FILE_MISSING"

    invalid = output_dir / "bad.mp4"
    invalid.write_bytes(b"not a video")
    job["renders"][1]["output_path"] = str(invalid)
    job_store.set("firstjob", job)
    assert client.post("/api/youtube/publish/firstjob/2").json()["detail"] == "INVALID_OUTPUT"

    outside = tmp_path / "outside.mp4"
    shutil.copy(renders[0]["output_path"], outside)
    job["renders"][1]["output_path"] = str(outside)
    job_store.set("firstjob", job)
    assert client.post("/api/youtube/publish/firstjob/2").json()["detail"] == "INVALID_OUTPUT"
    assert client.get("/api/jobs/firstjob/renders/2/media").json()["detail"] == "INVALID_OUTPUT"
    upload.assert_not_called()


def test_failure_keeps_completed_render_and_safe_error(completed_job, monkeypatch):
    client, renders, _ = completed_job
    monkeypatch.setattr(youtube_publisher, "publish_video",
                        Mock(side_effect=RuntimeError("sensitive external error")))
    response = client.post("/api/youtube/publish/firstjob/2")
    assert response.status_code == 500
    assert response.json()["detail"] == "UPLOAD_FAILED"
    persisted = JobStore().get("firstjob")
    assert persisted["renders"][1]["success"] is True
    assert persisted["renders"][1]["publish_status"] == "FAILED"
    assert persisted["renders"][1]["publish_error"] == "UPLOAD_FAILED"
    assert client.get("/api/jobs/firstjob/renders/2/media").status_code == 200


def test_success_persists_upload_then_cleans_only_that_local_render(completed_job, monkeypatch):
    client, renders, output_dir = completed_job
    monkeypatch.setattr(storage_lifecycle, "root", output_dir.parent)
    saved = job_store.get("firstjob")
    saved["lifecycle_version"] = 1
    job_store.set("firstjob", saved)
    observed = []

    def upload(**_kwargs):
        observed.append(Path(renders[0]["output_path"]).exists())
        return {"status": "UPLOADED", "video_id": "abcdefghijk", "published_at": 123.0}

    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    response = client.post("/api/youtube/publish/firstjob/1")
    assert response.status_code == 200
    assert observed == [True]
    persisted = job_store.get("firstjob")
    assert persisted["renders"][0]["publish_status"] == "UPLOADED"
    assert persisted["renders"][0]["youtube_video_id"] == "abcdefghijk"
    assert persisted["renders"][0]["output_path"] is None
    assert persisted["renders"][0]["storage_status"] == "CLEANED"
    assert not Path(renders[0]["output_path"]).exists()
    assert Path(renders[1]["output_path"]).exists()
    public = client.get("/api/status/firstjob").json()["renders"][0]
    assert public["storage_status"] == "CLEANED"
    assert public["youtube_video_id"] == "abcdefghijk"
    assert client.get("/api/jobs/firstjob/renders/1/media").status_code == 404
    assert client.post("/api/youtube/publish/firstjob/1").json()["video_id"] == "abcdefghijk"
    assert observed == [True]


def test_reauth_does_not_mark_other_render(completed_job, monkeypatch):
    client, _, _ = completed_job
    monkeypatch.setattr(youtube_publisher, "publish_video",
                        Mock(return_value={"status": "REAUTH_REQUIRED"}))
    response = client.post("/api/youtube/publish/firstjob/2")
    assert response.status_code == 401
    assert response.json()["detail"] == "REAUTH_REQUIRED"
    persisted = JobStore().get("firstjob")
    assert persisted["renders"][0].get("publish_status") is None
    assert persisted["renders"][1]["publish_status"] == "REAUTH_REQUIRED"


def test_job_id_traversal_is_rejected(completed_job):
    with pytest.raises(RenderResolutionError) as exc:
        resolve_render("../firstjob", 1)
    assert exc.value.code == "JOB_NOT_FOUND"


def test_mp4_without_audio_is_invalid(completed_job):
    _, _, output_dir = completed_job
    silent = output_dir / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=32x32:r=10", "-t", "0.4", "-c:v", "mpeg4",
         str(silent)], check=True, capture_output=True,
    )
    assert youtube_publisher.validate_file(silent) is False


def test_publish_all_is_sequential_and_persists_before_each_cleanup(completed_job, monkeypatch):
    client, renders, output_dir = completed_job
    monkeypatch.setattr(storage_lifecycle, "root", output_dir.parent)
    job = job_store.get("firstjob")
    job["lifecycle_version"] = 1
    job_store.set("firstjob", job)
    observed = []
    cleanup = storage_lifecycle.cleanup_render

    def checked_cleanup(job_id, rank):
        saved = job_store.get(job_id)
        item = next(r for r in saved["renders"] if r["rank"] == rank)
        assert item["publish_status"] == "UPLOADED"
        assert item["youtube_video_id"] == f"public{rank}"
        assert Path(item["output_path"]).exists()
        observed.append(("clean", rank))
        return cleanup(job_id, rank)

    def upload(**kwargs):
        rank = 1 if kwargs["file_path"] == Path(renders[0]["output_path"]) else 2
        observed.append(("upload", rank))
        return {"status": "UPLOADED", "video_id": f"public{rank}",
                "privacy_status": "public"}

    monkeypatch.setattr(storage_lifecycle, "cleanup_render", checked_cleanup)
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    response = client.post("/api/youtube/publish-all/firstjob")
    assert response.status_code == 200
    assert response.json()["status"] == "UPLOADED"
    assert [item["status"] for item in response.json()["results"]] == ["UPLOADED", "UPLOADED"]
    assert observed == [("upload", 1), ("clean", 1), ("upload", 2), ("clean", 2)]
    saved = job_store.get("firstjob")
    assert all(r["output_path"] is None for r in saved["renders"])
    assert all(r["youtube_privacy_status"] == "public" for r in saved["renders"])


def test_publish_all_repeated_request_skips_already_uploaded(completed_job, monkeypatch):
    client, _, _ = completed_job
    upload = Mock(side_effect=[
        {"status": "UPLOADED", "video_id": "first"},
        {"status": "UPLOADED", "video_id": "second"}])
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    assert client.post("/api/youtube/publish-all/firstjob").json()["status"] == "UPLOADED"
    second = client.post("/api/youtube/publish-all/firstjob").json()
    assert second["status"] == "NOTHING_TO_PUBLISH"
    assert [item["status"] for item in second["results"]] == ["ALREADY_UPLOADED", "ALREADY_UPLOADED"]
    assert upload.call_count == 2


def test_publish_all_continues_after_failure_and_keeps_failed_mp4(completed_job, monkeypatch):
    client, renders, output_dir = completed_job
    monkeypatch.setattr(storage_lifecycle, "root", output_dir.parent)
    job = job_store.get("firstjob")
    job["lifecycle_version"] = 1
    job_store.set("firstjob", job)
    upload = Mock(side_effect=[
        {"status": "FAILED", "error": "UPLOAD_FAILED"},
        {"status": "UPLOADED", "video_id": "second"}])
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    result = client.post("/api/youtube/publish-all/firstjob").json()
    assert result["status"] == "PARTIAL_SUCCESS"
    assert [item["status"] for item in result["results"]] == ["FAILED", "UPLOADED"]
    assert Path(renders[0]["output_path"]).exists()
    assert not Path(renders[1]["output_path"]).exists()
    saved = job_store.get("firstjob")
    assert saved["renders"][0]["publish_error_code"] == "UPLOAD_FAILED"
    assert saved["renders"][1]["youtube_video_id"] == "second"


def test_publish_all_cleans_source_and_keeps_failed_final(completed_job, monkeypatch):
    client, renders, output_dir = completed_job
    root = output_dir.parent
    monkeypatch.setattr(storage_lifecycle, "root", root)
    source = root / "input/firstjob_source.mp4"
    source.parent.mkdir()
    source.write_bytes(b"source")
    job = job_store.get("firstjob")
    job.update(filepath=str(source), lifecycle_version=1,
               source_cleanup_eligible=True,
               render_request={"candidates": [{"rank": 1}, {"rank": 2}]})
    job_store.set("firstjob", job)
    monkeypatch.setattr(youtube_publisher, "publish_video", Mock(side_effect=[
        {"status": "UPLOADED", "video_id": "abcdefghijk"},
        {"status": "FAILED", "error": "UPLOAD_FAILED"}]))
    response = client.post("/api/youtube/publish-all/firstjob")
    assert response.json()["status"] == "PARTIAL_SUCCESS"
    assert not Path(renders[0]["output_path"]).exists()
    assert Path(renders[1]["output_path"]).exists()
    assert not source.exists()
    saved = job_store.get("firstjob")
    assert saved["renders"][0]["youtube_video_id"] == "abcdefghijk"
    assert saved["source_storage_status"] == "CLEANED"


def test_publish_all_success_leaves_only_history(completed_job, monkeypatch):
    client, renders, output_dir = completed_job
    root = output_dir.parent
    monkeypatch.setattr(storage_lifecycle, "root", root)
    source = root / "input/firstjob_source.mp4"
    source.parent.mkdir()
    source.write_bytes(b"source")
    job = job_store.get("firstjob")
    job.update(filepath=str(source), lifecycle_version=1,
               source_cleanup_eligible=True,
               render_request={"candidates": [{"rank": 1}, {"rank": 2}]})
    job_store.set("firstjob", job)
    monkeypatch.setattr(youtube_publisher, "publish_video", Mock(side_effect=[
        {"status": "UPLOADED", "video_id": "abcdefghijk"},
        {"status": "UPLOADED", "video_id": "lmnopqrstuv"}]))
    response = client.post("/api/youtube/publish-all/firstjob")
    assert response.json()["status"] == "UPLOADED"
    assert not source.exists()
    assert all(not Path(r["output_path"]).exists() for r in renders)
    saved = job_store.get("firstjob")
    assert [r["youtube_video_id"] for r in saved["renders"]] == [
        "abcdefghijk", "lmnopqrstuv"]
    assert all(r["storage_status"] == "CLEANED" for r in saved["renders"])


def test_publish_persistence_failure_keeps_mp4(completed_job, monkeypatch):
    from app.services.youtube_publish_service import publish_one_locked
    _, renders, output_dir = completed_job
    monkeypatch.setattr(storage_lifecycle, "root", output_dir.parent)
    monkeypatch.setattr(youtube_publisher, "publish_video", Mock(return_value={
        "status": "UPLOADED", "video_id": "abcdefghijk"}))
    original_set = job_store.set

    def fail_durable(job_id, data, *, durable=False):
        if durable:
            raise OSError("persist failed")
        return original_set(job_id, data)

    monkeypatch.setattr(job_store, "set", fail_durable)
    with pytest.raises(OSError):
        publish_one_locked("firstjob", 1)
    assert Path(renders[0]["output_path"]).exists()


def test_missing_youtube_id_keeps_mp4(completed_job, monkeypatch):
    client, renders, output_dir = completed_job
    monkeypatch.setattr(storage_lifecycle, "root", output_dir.parent)
    monkeypatch.setattr(youtube_publisher, "publish_video", Mock(return_value={
        "status": "UPLOADED"}))
    assert client.post("/api/youtube/publish/firstjob/1").status_code == 500
    assert Path(renders[0]["output_path"]).exists()


def test_publish_all_concurrent_requests_do_not_duplicate(completed_job, monkeypatch):
    _, _, _ = completed_job
    uploads = []

    def upload(**kwargs):
        uploads.append(kwargs["file_path"].name)
        time.sleep(0.08)
        return {"status": "UPLOADED", "video_id": f"video{len(uploads)}"}

    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: TestClient(app).post(
            "/api/youtube/publish-all/firstjob").json(), range(2)))
    assert len(uploads) == 2
    assert sorted(item["status"] for item in responses) == ["NOTHING_TO_PUBLISH", "UPLOADED"]


def test_publish_all_reports_missing_and_invalid_mp4(completed_job, monkeypatch):
    client, renders, _ = completed_job
    Path(renders[0]["output_path"]).unlink()
    Path(renders[1]["output_path"]).write_bytes(b"invalid")
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    result = client.post("/api/youtube/publish-all/firstjob").json()
    assert result["status"] == "FAILED"
    assert [item["error_code"] for item in result["results"]] == [
        "OUTPUT_FILE_MISSING", "INVALID_OUTPUT"]
    assert Path(renders[1]["output_path"]).exists()
    upload.assert_not_called()


def test_publish_all_reauth_and_quota_keep_files(completed_job, monkeypatch):
    client, renders, _ = completed_job
    upload = Mock(side_effect=[
        {"status": "REAUTH_REQUIRED", "error": "REAUTH_REQUIRED"},
        {"status": "FAILED", "error": "QUOTA_EXCEEDED"}])
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    result = client.post("/api/youtube/publish-all/firstjob").json()
    assert result["status"] == "FAILED"
    assert [item["error_code"] for item in result["results"]] == [
        "REAUTH_REQUIRED", "QUOTA_EXCEEDED"]
    assert all(Path(r["output_path"]).exists() for r in renders)
    saved = job_store.get("firstjob")
    assert saved["renders"][0]["publish_status"] == "REAUTH_REQUIRED"
    assert saved["renders"][1]["publish_status"] == "FAILED"


@pytest.mark.parametrize("limit_code", [
    "QUOTA_EXCEEDED", "API_QUOTA_EXCEEDED", "CHANNEL_UPLOAD_LIMIT", "RATE_LIMITED",
])
def test_publish_all_stops_after_first_youtube_limit_and_retains_untried_clips(
        completed_job, monkeypatch, limit_code):
    client, renders, _ = completed_job
    upload = Mock(return_value={"status": "FAILED", "error": limit_code})
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)

    result = client.post("/api/youtube/publish-all/firstjob").json()
    assert result["status"] == "FAILED"
    assert result["results"] == [
        {"rank": 1, "status": "FAILED", "error_code": limit_code},
        {"rank": 2, "status": "DEFERRED", "error_code": limit_code},
    ]
    upload.assert_called_once()
    saved = job_store.get("firstjob")
    assert saved["renders"][0]["publish_error_code"] == limit_code
    assert saved["renders"][1].get("publish_status") is None
    assert all(Path(render["output_path"]).is_file() for render in renders)

    upload.side_effect = [
        {"status": "UPLOADED", "video_id": "recovered-one"},
        {"status": "UPLOADED", "video_id": "recovered-two"},
    ]
    resumed = client.post("/api/youtube/publish-all/firstjob").json()
    assert resumed["status"] == "UPLOADED"
    assert [item["status"] for item in resumed["results"]] == ["UPLOADED", "UPLOADED"]
    assert all(render.get("youtube_video_id") for render in job_store.get("firstjob")["renders"])


def test_single_publish_reports_limit_without_deleting_mp4(completed_job, monkeypatch):
    client, renders, _ = completed_job
    monkeypatch.setattr(youtube_publisher, "publish_video", Mock(return_value={
        "status": "FAILED", "error": "CHANNEL_UPLOAD_LIMIT"}))
    response = client.post("/api/youtube/publish/firstjob/1")
    assert response.status_code == 429
    assert response.json()["detail"] == "CHANNEL_UPLOAD_LIMIT"
    assert Path(renders[0]["output_path"]).is_file()


def test_metadata_is_reused_after_failed_publication_and_reload(completed_job, monkeypatch):
    client, _, _ = completed_job
    uploads = []

    def upload(**kwargs):
        uploads.append(kwargs)
        return {"status": "FAILED", "error": "UPLOAD_FAILED"} if len(uploads) == 1 else {
            "status": "UPLOADED", "video_id": "retry-success"}

    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    assert client.post("/api/youtube/publish/firstjob/1").status_code == 500
    first = JobStore().get("firstjob")["renders"][0]["youtube_metadata"]
    assert first["title"] == uploads[0]["title"]
    job = job_store.get("firstjob")
    job["candidates"] = [{"rank": 1, "title": "A different title"}]
    job_store.set("firstjob", job)
    assert client.post("/api/youtube/publish/firstjob/1").status_code == 200
    assert uploads[1]["title"] == first["title"]


def test_unfinished_job_cannot_publish_automatically_or_manually(completed_job, monkeypatch):
    client, _, _ = completed_job
    job = job_store.get("firstjob")
    job["status"] = "RENDERING"
    job_store.set("firstjob", job)
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    assert client.post("/api/youtube/publish/firstjob/1").status_code == 409
    assert client.post("/api/youtube/publish-all/firstjob").status_code == 409
    upload.assert_not_called()


def test_actual_private_visibility_is_recorded_if_youtube_forces_it(completed_job, monkeypatch):
    client, _, _ = completed_job
    monkeypatch.setattr(youtube_publisher, "publish_video", Mock(return_value={
        "status": "UPLOADED", "video_id": "private123",
        "privacy_status": "private"}))
    response = client.post("/api/youtube/publish/firstjob/1")
    assert response.status_code == 200
    assert job_store.get("firstjob")["renders"][0]["youtube_privacy_status"] == "private"


def test_youtube_source_rights_block_publication_without_deleting_mp4(completed_job, monkeypatch):
    from app.services.youtube_service import youtube_service
    client, renders, _ = completed_job
    job = job_store.get("firstjob")
    job["source_id"] = "youtube_abcdefghijk"
    job_store.set("firstjob", job)
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id: {
        "rights_status": "RESTRICTED"})
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    single = client.post("/api/youtube/publish/firstjob/1")
    assert single.status_code == 403
    assert single.json()["detail"] == "RIGHTS_BLOCKED"
    bulk = client.post("/api/youtube/publish-all/firstjob").json()
    assert bulk["status"] == "FAILED"
    assert all(item["error_code"] == "RIGHTS_BLOCKED" for item in bulk["results"])
    assert all(Path(r["output_path"]).exists() for r in renders)
    upload.assert_not_called()


def test_single_and_bulk_publish_each_persisted_source_notice(completed_job, monkeypatch):
    from app.services.youtube_service import youtube_service
    from app.services.youtube_metadata import build_youtube_metadata
    client, _, _ = completed_job
    job = job_store.get("firstjob")
    job.update(source_id="youtube_abcdefghijk", source_visibility="public",
               source_video_id="abcdefghijk", source_video_title="Original video",
               source_channel_name="Original creator", source_channel_id="UCoriginal",
               rights_status="UNKNOWN", rights_evidence="STANDARD_YOUTUBE_LICENSE")
    for render in job["renders"]:
        rank = render["rank"]
        render["youtube_metadata"] = build_youtube_metadata(job, render, {
            "rank": rank, "title": f"Moment {rank}",
            "short_description": f"Distinct moment {rank}.",
            "topic": f"Moment {rank}", "content_type": "STORY", "categories": []})
    job_store.set("firstjob", job)
    saved_descriptions = {render["rank"]: render["youtube_metadata"]["description"]
                          for render in job["renders"]}
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id: {
        "rights_status": "UNKNOWN", "privacy_status": "public", "upload_status": "processed"})
    calls = []

    def upload(**kwargs):
        calls.append(kwargs)
        return {"status": "UPLOADED", "video_id": f"uploaded{len(calls)}",
                "privacy_status": "public"}

    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    assert client.post("/api/youtube/publish/firstjob/1").status_code == 200
    assert client.post("/api/youtube/publish-all/firstjob").status_code == 200
    assert len(calls) == 2
    assert calls[0]["description"] == saved_descriptions[1]
    assert calls[1]["description"] == saved_descriptions[2]
    assert calls[0]["description"] != calls[1]["description"]
    assert all("⚠️ Copyright & Source Notice:" in call["description"] for call in calls)
    assert all("https://www.youtube.com/watch?v=abcdefghijk" in call["description"] for call in calls)
    assert all(render["youtube_metadata"]["description"] == saved_descriptions[render["rank"]]
               for render in JobStore().get("firstjob")["renders"])
    assert config.YOUTUBE_DEFAULT_PRIVACY == "public"


def test_youtube_publication_requires_persisted_attribution(completed_job, monkeypatch):
    from app.services.youtube_service import youtube_service
    client, renders, _ = completed_job
    job = job_store.get("firstjob")
    job.update(source_id="youtube_abcdefghijk", source_visibility="public",
               source_video_id="abcdefghijk", source_video_title="Original video",
               rights_status="UNKNOWN")
    job_store.set("firstjob", job)
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id: {
        "rights_status": "UNKNOWN", "privacy_status": "public", "upload_status": "processed"})
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)
    response = client.post("/api/youtube/publish/firstjob/1")
    assert response.status_code == 409
    assert response.json()["detail"] == "ATTRIBUTION_MISSING"
    assert Path(renders[0]["output_path"]).exists()
    upload.assert_not_called()


def test_placeholder_cannot_be_uploaded_and_can_be_automatically_recovered(completed_job, monkeypatch):
    client, renders, _ = completed_job
    from app.api import routes
    from app.services import clip_metadata_service
    upload = Mock(return_value={'status': 'UPLOADED', 'video_id': 'generated1'})
    monkeypatch.setattr(youtube_publisher, 'publish_video', upload)
    monkeypatch.setattr(routes.heavy_gate, 'claim', Mock())
    monkeypatch.setattr(routes.heavy_gate, 'release', Mock())
    job = job_store.get('firstjob')
    job['renders'][0]['title'] = 'Source excerpt 06:40–07:00'
    job_store.set('firstjob', job)
    blocked = client.post('/api/youtube/publish/firstjob/1')
    assert blocked.status_code == 409
    assert blocked.json()['detail'] == 'TITLE_REVIEW_REQUIRED'
    upload.assert_not_called()
    assert Path(renders[0]['output_path']).exists()
    monkeypatch.setattr(clip_metadata_service, 'describe_clip', Mock(return_value={
        'title': 'He lifts the bar with one hand', 'short_description': 'An athlete lifts a bar with one hand.', 'topic': 'Gym'}))
    response = client.post('/api/jobs/firstjob/renders/1/metadata/generate')
    assert response.status_code == 200
    assert response.json()['metadata_status'] == 'GENERATED'
    assert client.post('/api/youtube/publish/firstjob/1').json()['status'] == 'UPLOADED'
    assert upload.call_args.kwargs['title'] == 'He lifts the bar with one hand'
    routes.heavy_gate.release.assert_called_once_with('firstjob')


def test_pipeline_admission_rejects_a_concurrent_publisher(completed_job):
    from app.services.youtube_publish_service import publishing_lock
    client, _, _ = completed_job
    with publishing_lock('firstjob'):
        response = client.post('/api/render/firstjob', json={'filename': 'source.mp4', 'candidates': [], 'settings': {}})
    assert response.status_code == 409
    assert response.json()['detail'] == 'JOB_BUSY'
