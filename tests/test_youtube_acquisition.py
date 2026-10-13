import shutil
import subprocess
import time
import threading
import io
from pathlib import Path

import pytest

from app.config import config
from app.services import youtube_acquisition
from app.services.download_service import DownloadService
from app.services.job_store import job_store
from app.services.source_store import source_store
from app.services.scheduler import scheduler
from app.services.heavy_gate import ProcessingBusy
from app.services.storage_lifecycle import InsufficientStorage, storage_lifecycle


VIDEO_ONE = "abcdefghijk"
VIDEO_TWO = "lmnopqrstuv"


@pytest.fixture
def setup_download(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_acquisition, "local_youtube_session", lambda: None)
    jobs = tmp_path / "jobs"
    registry = tmp_path / "sources" / "registry"
    jobs.mkdir()
    registry.mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_JOBS", jobs)
    monkeypatch.setattr(job_store, "jobs_dir", jobs)
    monkeypatch.setattr(source_store, "registry_dir", registry)
    sample = tmp_path / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=blue:s=32x32:r=10", "-f", "lavfi", "-i",
         "anullsrc=r=44100:cl=mono", "-t", "0.4", "-c:v", "mpeg4",
         "-c:a", "aac", "-shortest", str(sample)],
        check=True, capture_output=True,
    )
    service = DownloadService(tmp_path)
    yield service, sample
    service.stop()


def fake_extractor(monkeypatch, sample, outcomes):
    calls = []

    class FakeDownloader:
        def __init__(self, options):
            self.options = options
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, url, download):
            idx = len(calls) - 1
            outcome = outcomes[idx] if idx < len(outcomes) else outcomes[-1]
            path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            if isinstance(outcome, Exception):
                (path.parent / "source.mp4.part").write_bytes(b"partial")
                raise outcome
            if outcome == "zero":
                path.write_bytes(b"")
            elif outcome == "corrupt":
                path.write_bytes(b"invalid mp4")
            else:
                shutil.copy(sample, path)
            return {"title": "Sample", "duration": 0.4, "uploader": "Test"}

    monkeypatch.setattr(youtube_acquisition.yt_dlp, "YoutubeDL", FakeDownloader)
    return calls


def await_terminal(service, dl_id):
    for _ in range(300):
        state = service.get_status(dl_id)
        if state.get("status") in ("COMPLETED", "FAILED", "AUTH_REQUIRED",
                                   "ACQUISITION_BLOCKED", "RIGHTS_BLOCKED", "UNAVAILABLE"):
            return state
        time.sleep(0.01)
    pytest.fail("acquisition did not reach a terminal state")


def test_valid_download_registers_source_and_job_once(setup_download, monkeypatch):
    service, sample = setup_download
    assert service.acquisition_state(VIDEO_ONE) == "UNVERIFIED"
    calls = fake_extractor(monkeypatch, sample, ["valid"])
    dl_id = service.enqueue_download(
        VIDEO_ONE, "Example", rights_status="LICENSED", public_verified=True,
        rights_evidence="YOUTUBE_CREATIVE_COMMONS",
        source_video_title="Example",
        source_channel_name="Verified creator", source_channel_id="UCverified")
    state = await_terminal(service, dl_id)
    assert state["status"] == "COMPLETED"
    assert state["job_id"] == f"job_youtube_{VIDEO_ONE}"
    assert len(calls) == 1
    assert calls[0]["cachedir"] == str(service.cache_dir)
    assert "cache_dir" not in calls[0]
    assert calls[0]["js_runtimes"] == {"deno": {"path": str(config.PROJECT_ROOT / "bin" / "deno")}}
    source = source_store.get(f"youtube_{VIDEO_ONE}")
    assert source.acquisition_status == "COMPLETED"
    assert source.rights_status == "LICENSED"
    assert source.rights_evidence == "YOUTUBE_CREATIVE_COMMONS"
    assert source.source_video_id == VIDEO_ONE
    assert source.source_video_title == "Example"
    assert source.source_channel_name == "Verified creator"
    assert source.source_channel_id == "UCverified"
    assert source.source_url == f"https://www.youtube.com/watch?v={VIDEO_ONE}"
    assert Path(source.source_path).read_bytes() == sample.read_bytes()
    saved_job = job_store.get(state["job_id"])
    assert saved_job["status"] == "QUEUED_ANALYSIS"
    assert saved_job["source_video_id"] == source.source_video_id
    assert saved_job["source_video_title"] == source.source_video_title
    assert saved_job["source_channel_name"] == source.source_channel_name
    assert saved_job["source_channel_id"] == source.source_channel_id
    assert saved_job["source_url"] == source.source_url
    assert saved_job["rights_evidence"] == source.rights_evidence
    from app.api.routes import get_sources
    assert any(item["source_id"] == f"youtube_{VIDEO_ONE}" for item in get_sources()["sources"])
    assert not (service.temp_dir / dl_id).exists()

    second = service.enqueue_download(VIDEO_ONE, "Example", rights_status="LICENSED", public_verified=True)
    assert service.get_status(second)["job_id"] == state["job_id"]
    assert len(calls) == 1
    assert len(job_store.get_all()) == 1


def test_preexisting_cached_source_is_not_marked_cleanup_eligible(setup_download):
    service, sample = setup_download
    old_source = service.trends_dir / VIDEO_ONE / "source.mp4"
    old_source.parent.mkdir(parents=True)
    shutil.copy(sample, old_source)
    state = service.get_status(service.enqueue_download(VIDEO_ONE, "Existing", rights_status="LICENSED", public_verified=True))
    assert state["status"] == "COMPLETED"
    assert source_store.get(f"youtube_{VIDEO_ONE}").storage_lifecycle_version == 0
    assert job_store.get(state["job_id"])["source_cleanup_eligible"] is False


@pytest.mark.parametrize("outcome", ["zero", "corrupt"])
def test_invalid_output_never_registers_source(setup_download, monkeypatch, outcome):
    service, sample = setup_download
    calls = fake_extractor(monkeypatch, sample, [outcome] * 4)
    monkeypatch.setattr(youtube_acquisition, "po_provider_ready", lambda: True)
    dl_id = service.enqueue_download(VIDEO_ONE, "Invalid", rights_status="LICENSED", public_verified=True)
    state = await_terminal(service, dl_id)
    assert state["status"] == "FAILED"
    assert state["error_code"] == "INVALID_OUTPUT"
    assert len(calls) == 2
    assert source_store.get(f"youtube_{VIDEO_ONE}") is None
    assert job_store.get_all() == []
    assert not (service.temp_dir / dl_id).exists()


@pytest.mark.parametrize("message,status,attempts", [
    ("Private video", "AUTH_REQUIRED", 1),
    ("Copyright restriction", "RIGHTS_BLOCKED", 1),
    ("Video unavailable", "UNAVAILABLE", 1),
    ("HTTP Error 400", "FAILED", 1),
    ("HTTP Error 403", "ACQUISITION_BLOCKED", 2),
])
def test_classified_failure_has_bounded_fallback_and_cleans_partial(
        setup_download, monkeypatch, message, status, attempts):
    service, sample = setup_download
    calls = fake_extractor(monkeypatch, sample, [RuntimeError(message)] * attempts)
    monkeypatch.setattr(youtube_acquisition, "po_provider_ready", lambda: True)
    dl_id = service.enqueue_download(VIDEO_ONE, "Blocked", rights_status="LICENSED", public_verified=True)
    state = await_terminal(service, dl_id)
    assert state["status"] == status
    assert len(calls) == attempts
    assert not (service.temp_dir / dl_id).exists()
    assert source_store.get(f"youtube_{VIDEO_ONE}") is None
    assert job_store.get_all() == []
    assert all("cookiefile" not in options and "cookiesfrombrowser" not in options
               for options in calls)


def test_po_retry_uses_provider_then_succeeds(setup_download, monkeypatch):
    service, sample = setup_download
    calls = fake_extractor(monkeypatch, sample, [RuntimeError("HTTP Error 403"), "valid"])
    monkeypatch.setattr(youtube_acquisition, "po_provider_ready", lambda: True)
    state = await_terminal(service, service.enqueue_download(VIDEO_ONE, "Retry", rights_status="LICENSED", public_verified=True))
    assert state["status"] == "COMPLETED"
    assert state["strategy"] == "PO_TOKEN"
    assert len(calls) == 2
    assert calls[1]["extractor_args"]["youtube"]["player_client"] == ["mweb"]


def test_terminal_failure_is_persisted_and_skipped(setup_download, monkeypatch):
    service, sample = setup_download
    calls = fake_extractor(monkeypatch, sample, [RuntimeError("Private video")])
    first = await_terminal(service, service.enqueue_download(VIDEO_ONE, "Blocked", rights_status="LICENSED", public_verified=True))
    assert first["status"] == "AUTH_REQUIRED"
    next_id = service.enqueue_download(VIDEO_ONE, "Blocked", rights_status="LICENSED", public_verified=True)
    assert service.get_status(next_id)["status"] == "AUTH_REQUIRED"
    assert len(calls) == 1
    assert service._status_path(VIDEO_ONE).exists()
    restarted = DownloadService(service.project_root)
    try:
        assert restarted.get_status(next_id)["status"] == "AUTH_REQUIRED"
        assert restarted.acquisition_state(VIDEO_ONE) == "AUTH_REQUIRED"
    finally:
        restarted.stop()


def test_po_provider_unavailable_stops_after_one_attempt(setup_download, monkeypatch):
    service, sample = setup_download
    calls = fake_extractor(monkeypatch, sample, [RuntimeError("HTTP Error 403")] * 4)
    monkeypatch.setattr(youtube_acquisition, "po_provider_ready", lambda: False)
    state = await_terminal(service, service.enqueue_download(VIDEO_ONE, "Blocked", rights_status="LICENSED", public_verified=True))
    assert state["status"] == "ACQUISITION_BLOCKED"
    assert state.get("error_code") == "PO_TOKEN_PROVIDER_UNAVAILABLE" or state.get("error") == "PO_TOKEN_PROVIDER_UNAVAILABLE"
    assert state["attempted_strategies"] == ["STANDARD_YTDLP"]
    assert state["yt_dlp_exit_status"] is None
    assert len(calls) == 1


def test_provider_health_requires_plugin_registration_and_versioned_ping(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(youtube_acquisition.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response(b'{"version":"2.0.0"}'))
    assert youtube_acquisition.po_provider_ready()
    monkeypatch.setattr(youtube_acquisition.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: Response(b'not-json'))
    assert not youtube_acquisition.po_provider_ready()


def test_po_provider_warning_is_sanitized_and_attempts_persisted(setup_download, monkeypatch):
    service, _ = setup_download
    calls = []

    class WarningDownloader:
        def __init__(self, options):
            calls.append(options)
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, *_args, **_kwargs):
            if len(calls) == 2:
                self.options["logger"].warning("Error reaching POST /get_pot; secret=must-not-persist")
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(youtube_acquisition.yt_dlp, "YoutubeDL", WarningDownloader)
    monkeypatch.setattr(youtube_acquisition, "po_provider_ready", lambda: True)
    state = await_terminal(service, service.enqueue_download(VIDEO_ONE, "Blocked", rights_status="LICENSED", public_verified=True))
    assert state["attempted_strategies"] == ["STANDARD_YTDLP", "PO_TOKEN"]
    assert state["error_code"] == "PO_TOKEN_PROVIDER_UNAVAILABLE"
    assert [attempt["code"] for attempt in state["attempt_diagnostics"]] == ["BOT_PROTECTION", "PO_TOKEN_PROVIDER_UNAVAILABLE"]
    assert "must-not-persist" not in str(state)


def test_invalid_video_id_cannot_escape_storage(setup_download):
    service, _ = setup_download
    with pytest.raises(ValueError, match="INVALID_VIDEO_ID"):
        service.enqueue_download("../wrong/path", "Bad")


def test_unverified_rights_cannot_start_service_acquisition(setup_download):
    service, _ = setup_download
    with pytest.raises(ValueError, match="RIGHTS_UNVERIFIED"):
        service.enqueue_download(VIDEO_ONE, "Unknown rights")
    with pytest.raises(ValueError, match="RIGHTS_BLOCKED"):
        service.enqueue_download(VIDEO_ONE, "Restricted", rights_status="RESTRICTED")
    assert not service.active_downloads


def test_successful_reacquisition_repairs_existing_failed_registration(setup_download):
    service, sample = setup_download
    source = service.trends_dir / VIDEO_ONE / "source.mp4"
    source.parent.mkdir(parents=True)
    shutil.copy(sample, source)
    job_id = service._register_source_and_job(
        VIDEO_ONE, "Old title", "LICENSED")
    failed = job_store.get(job_id)
    failed["status"] = "FAILED"
    failed["filepath"] = None
    job_store.set(job_id, failed)
    service._register_source_and_job(
        VIDEO_ONE, "Current title", "LICENSED",
        new_acquisition=True, source_channel="Known Creator")
    repaired = job_store.get(job_id)
    asset = source_store.get(f"youtube_{VIDEO_ONE}")
    assert repaired["status"] == "QUEUED_ANALYSIS"
    assert repaired["filepath"] == str(source)
    assert repaired["source_cleanup_eligible"] is True
    assert repaired["source_channel"] == "Known Creator"
    assert asset.source_channel == "Known Creator"
    assert asset.storage_lifecycle_version == 1


def test_cached_legacy_job_is_not_implicitly_enrolled_in_cleanup(setup_download):
    service, sample = setup_download
    source = service.trends_dir / VIDEO_ONE / "source.mp4"
    source.parent.mkdir(parents=True)
    shutil.copy(sample, source)
    job_id = service._register_source_and_job(
        VIDEO_ONE, "Legacy title", "LICENSED")
    old = job_store.get(job_id)
    old.pop("lifecycle_version")
    old["status"] = "FAILED"
    job_store.set(job_id, old)
    service._register_source_and_job(
        VIDEO_ONE, "Legacy title", "LICENSED")
    saved = job_store.get(job_id)
    assert "lifecycle_version" not in saved
    assert saved["source_cleanup_eligible"] is False


def test_second_video_is_busy_while_first_downloads(setup_download, monkeypatch):
    service, sample = setup_download
    started = threading.Event()
    release = threading.Event()

    class SlowDownloader:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, *_args, **_kwargs):
            started.set()
            assert release.wait(3)
            path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            shutil.copy(sample, path)
            return {"title": "Sample", "duration": 0.4}

    monkeypatch.setattr(youtube_acquisition.yt_dlp, "YoutubeDL", SlowDownloader)
    first = service.enqueue_download(VIDEO_ONE, "First", rights_status="LICENSED", public_verified=True)
    assert started.wait(3)
    assert service.enqueue_download(VIDEO_ONE, "First", rights_status="LICENSED", public_verified=True) == first
    with pytest.raises(ProcessingBusy):
        service.enqueue_download(VIDEO_TWO, "Second", rights_status="LICENSED", public_verified=True)
    release.set()
    assert await_terminal(service, first)["status"] == "COMPLETED"


def test_low_disk_rejects_download_before_ytdlp(setup_download, monkeypatch):
    service, _ = setup_download
    monkeypatch.setattr(storage_lifecycle, "require_acquisition_space",
                        lambda: (_ for _ in ()).throw(InsufficientStorage("INSUFFICIENT_STORAGE")))
    with pytest.raises(InsufficientStorage):
        service.enqueue_download(VIDEO_ONE, "Blocked", rights_status="LICENSED", public_verified=True)
    assert not service.active_downloads


def test_acquisition_reconciles_and_rechecks_free_space_before_guard(setup_download, monkeypatch):
    service, _ = setup_download
    events = []
    free = {"bytes": 1}

    def cleanup():
        events.append("cleanup")
        free["bytes"] = 2

    def guard():
        events.append("guard")
        assert free["bytes"] == 2

    monkeypatch.setattr(storage_lifecycle, "prepare_for_new_video", cleanup)
    monkeypatch.setattr(storage_lifecycle, "require_acquisition_space", guard)
    monkeypatch.setattr(service.executor, "submit", lambda *_args: None)
    service.enqueue_download(VIDEO_ONE, "Ready", rights_status="LICENSED",
                             public_verified=True)
    assert events == ["cleanup", "guard"]


def test_scheduler_skips_blocked_candidate_and_respects_rights(monkeypatch):
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service

    monkeypatch.setattr(config, "YOUTUBE_DATA_API_KEY", "test-key")
    monkeypatch.setattr(config, "AUTO_PROCESS_ELIGIBLE_SOURCES", True)
    monkeypatch.setattr(youtube_service, "get_us_trends", lambda **_: [
        {"id": VIDEO_ONE, "title": "Blocked", "rights_status": "LICENSED",
         "privacy_status": "public", "upload_status": "processed"},
        {"id": "zzzzzzzzzzz", "title": "Restricted", "rights_status": "RESTRICTED",
         "privacy_status": "public", "upload_status": "processed"},
        {"id": VIDEO_TWO, "title": "Next", "rights_status": "LICENSED",
         "privacy_status": "public", "upload_status": "processed"},
    ])
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda video_id: {
        "rights_status": "LICENSED", "privacy_status": "public", "upload_status": "processed", "title":
        "Blocked" if video_id == VIDEO_ONE else "Next"})
    monkeypatch.setattr(job_store, "get", lambda _job_id: None)
    calls = []

    def enqueue(video_id, title, **kwargs):
        calls.append((video_id, kwargs["rights_status"]))
        return video_id

    monkeypatch.setattr(download_service, "enqueue_download", enqueue)
    monkeypatch.setattr(download_service, "get_status", lambda video_id: {
        "status": "ACQUISITION_BLOCKED" if video_id == VIDEO_ONE else "QUEUED"})
    scheduler._run_once()
    assert calls == [(VIDEO_ONE, "LICENSED"),
                     (VIDEO_TWO, "LICENSED")]


def test_rights_restricted_trend_cannot_start_acquisition(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id:
                        {"rights_status": "RESTRICTED", "privacy_status": "private",
                         "upload_status": "processed", "title": "Restricted"})
    calls = []
    monkeypatch.setattr(download_service, "enqueue_download", lambda *_args, **_kwargs: calls.append(True))
    response = TestClient(app).post("/api/download/youtube", json={
        "video_id": VIDEO_ONE, "title": "Restricted"})
    assert response.status_code == 403
    assert response.json()["detail"] == "RIGHTS_BLOCKED"
    assert calls == []


def test_unknown_public_trend_can_start_acquisition(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id:
                        {"rights_status": "UNKNOWN", "rights_evidence": "STANDARD_YOUTUBE_LICENSE",
                         "privacy_status": "public", "upload_status": "processed",
                         "title": "Verified title", "channel": "Verified channel", "channel_id": "UCverified"})
    calls = []
    monkeypatch.setattr(download_service, "enqueue_download", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or "download-id")
    response = TestClient(app).post("/api/download/youtube", json={"video_id": VIDEO_ONE})
    assert response.status_code == 200
    assert response.json()["dl_id"] == "download-id"
    assert calls[0][1]["rights_status"] == "UNKNOWN"
    assert calls[0][1]["rights_evidence"] == "STANDARD_YOUTUBE_LICENSE"
    assert calls[0][1]["source_video_title"] == "Verified title"
    assert calls[0][1]["source_channel_id"] == "UCverified"
    assert calls[0][1]["public_verified"] is True


def test_public_unknown_stays_unknown_through_source_and_job(setup_download, monkeypatch):
    service, sample = setup_download
    fake_extractor(monkeypatch, sample, ["valid"])
    dl_id = service.enqueue_download(
        VIDEO_ONE, "Public example", rights_status="UNKNOWN",
        rights_evidence="STANDARD_YOUTUBE_LICENSE", public_verified=True,
        source_channel_name="Actual channel", source_channel_id="UCactual")
    state = await_terminal(service, dl_id)
    assert state["status"] == "COMPLETED"
    asset = source_store.get(f"youtube_{VIDEO_ONE}")
    job = job_store.get(state["job_id"])
    assert asset.rights_status == job["rights_status"] == "UNKNOWN"
    assert asset.rights_evidence == job["rights_evidence"] == "STANDARD_YOUTUBE_LICENSE"
    assert asset.source_visibility == job["source_visibility"] == "public"
    assert asset.source_channel_name == job["source_channel_name"] == "Actual channel"
    assert asset.source_channel_id == job["source_channel_id"] == "UCactual"
    assert asset.source_url == job["source_url"] == f"https://www.youtube.com/watch?v={VIDEO_ONE}"


@pytest.mark.parametrize("verified", [
    None,
    {"rights_status": "UNKNOWN", "privacy_status": "private", "upload_status": "processed"},
    {"rights_status": "UNKNOWN", "privacy_status": "public", "upload_status": "deleted"},
    {"rights_status": "UNKNOWN", "privacy_status": "unlisted", "upload_status": "processed"},
])
def test_nonpublic_or_unavailable_video_never_starts_acquisition(monkeypatch, verified):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id: verified)
    calls = []
    monkeypatch.setattr(download_service, "enqueue_download", lambda *_args, **_kwargs: calls.append(True))
    response = TestClient(app).post("/api/download/youtube", json={"video_id": VIDEO_ONE})
    assert response.status_code in (403, 404)
    assert calls == []


def test_known_technical_block_prevents_another_attempt(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _video_id: {
        "rights_status": "UNKNOWN", "privacy_status": "public", "upload_status": "processed"})
    monkeypatch.setattr(download_service, "technical_downloadability", lambda _video_id: "BLOCKED")
    calls = []
    monkeypatch.setattr(download_service, "enqueue_download", lambda *_args, **_kwargs: calls.append(True))
    response = TestClient(app).post("/api/download/youtube", json={"video_id": VIDEO_ONE})
    assert response.status_code == 409
    assert response.json()["detail"] == "ACQUISITION_BLOCKED"
    assert calls == []


def test_eligible_trend_reaches_acquisition_with_verified_rights(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda video_id: {
        "rights_status": "LICENSED",
        "privacy_status": "public", "upload_status": "processed",
        "title": "Verified title", "channel": "Creator"} if video_id == VIDEO_ONE else None)
    calls = []
    monkeypatch.setattr(download_service, "enqueue_download", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or "download-id")
    response = TestClient(app).post("/api/download/youtube", json={
        "video_id": VIDEO_ONE, "title": "UI title"})
    assert response.status_code == 200
    assert response.json() == {"dl_id": "download-id"}
    assert calls == [((VIDEO_ONE, "Verified title"), {
        "rights_status": "LICENSED", "rights_evidence": "",
        "source_video_title": "Verified title",
        "source_channel_name": "Creator", "source_channel_id": "",
        "public_verified": True, "force_retry": True})]


def test_rights_verification_uses_current_video_license(monkeypatch):
    from unittest.mock import Mock
    from app.services.youtube_service import youtube_service
    client = Mock()
    client.videos().list().execute.return_value = {"items": [{
        "id": VIDEO_ONE, "status": {"license": "creativeCommon", "privacyStatus": "public",
                                    "uploadStatus": "processed"},
        "snippet": {"title": "Current title", "channelTitle": "Current creator",
                    "channelId": "UCcurrent"}}]}
    monkeypatch.setattr(youtube_service, "_get_client", lambda: client)
    result = youtube_service.get_video_rights(VIDEO_ONE)
    assert client.videos().list.call_args.kwargs == {
        "part": "status,snippet", "id": VIDEO_ONE, "maxResults": 1}
    assert result == {"rights_status": "LICENSED",
                      "rights_reason": "YOUTUBE_CREATIVE_COMMONS",
                      "rights_evidence": "YOUTUBE_CREATIVE_COMMONS",
                      "title": "Current title", "channel": "Current creator",
                      "channel_id": "UCcurrent", "privacy_status": "public",
                      "upload_status": "processed"}

@pytest.mark.parametrize('cached', [False, True])
def test_auto_edit_target_survives_real_media_registration(setup_download, monkeypatch, cached):
    from app.services.auto_edit_director import AutoEditDirector
    service,sample=setup_download
    options=AutoEditDirector.settings(target_count=5,count_mode='TARGET').model_dump()
    calls=fake_extractor(monkeypatch,sample,['valid'])
    if cached:
        final=service.trends_dir/VIDEO_ONE/'source.mp4'
        final.parent.mkdir(parents=True)
        shutil.copy(sample,final)
    state=await_terminal(service,service.enqueue_download(VIDEO_ONE,'Creator',rights_status='UNKNOWN',
        public_verified=True,auto_edit_settings=options))
    assert state['status']=='COMPLETED'
    assert len(calls)==(0 if cached else 1)
    asset=source_store.get(f'youtube_{VIDEO_ONE}')
    assert service._validate_with_ffprobe(Path(asset.source_path))
    assert asset.requested_clip_count==5 and asset.count_mode=='TARGET'
    job=job_store.get(state['job_id'])
    assert job['analysis_settings']==options
    assert job['status']=='QUEUED_ANALYSIS' and job['auto_render'] is False
    # Duplicate acquisition follows the already queued Auto Edit without a second transfer.
    assert service.enqueue_download(VIDEO_ONE,'Creator',rights_status='UNKNOWN',
        public_verified=True,auto_edit_settings=options)==state['id']
    with pytest.raises(ProcessingBusy):
        service.enqueue_download(VIDEO_ONE,'Creator',rights_status='UNKNOWN',public_verified=True,
            auto_edit_settings=AutoEditDirector.settings(target_count=3,count_mode='TARGET').model_dump())


def test_registry_source_at_noncanonical_path_is_reused(setup_download, monkeypatch):
    from app.models.schemas import SourceAsset
    from app.services.auto_edit_director import AutoEditDirector
    service,sample=setup_download
    asset=SourceAsset(source_id=f'youtube_{VIDEO_ONE}',provider='youtube',provider_video_id=VIDEO_ONE,
        title='Existing',source_path=str(sample),duration=.4,width=32,height=32,created_at=time.time(),
        video_codec='mpeg4',audio_codec='aac',rights_status='UNKNOWN',acquisition_status='COMPLETED',processing_status='READY')
    source_store.set(asset)
    calls=fake_extractor(monkeypatch,sample,['valid'])
    options=AutoEditDirector.settings(target_count=5,count_mode='TARGET').model_dump()
    state=await_terminal(service,service.enqueue_download(VIDEO_ONE,'Existing',rights_status='UNKNOWN',
        public_verified=True,auto_edit_settings=options))
    assert state['strategy']=='CACHED' and not calls
    assert job_store.get(state['job_id'])['filepath']==str(sample)
    assert source_store.get(asset.source_id).requested_clip_count==5


@pytest.mark.parametrize("code", ["BOT_PROTECTION", "HTTP_403", "HTTP_429", "PO_TOKEN_PROVIDER_UNAVAILABLE", "SESSION_EXPIRED"])
def test_public_video_can_retry_previous_temporary_block(monkeypatch, code):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.youtube_service import youtube_service
    from app.services.download_service import download_service
    monkeypatch.setattr(youtube_service, "get_video_rights", lambda _: {
        "rights_status":"UNKNOWN", "privacy_status":"public", "upload_status":"processed"})
    monkeypatch.setattr(download_service, "technical_downloadability", lambda _: "BLOCKED")
    monkeypatch.setattr(download_service, "acquisition_diagnostic", lambda _: code)
    calls=[]
    monkeypatch.setattr(download_service, "enqueue_download", lambda *args, **kwargs: calls.append(kwargs) or "retry-id")
    response=TestClient(app).post("/api/download/youtube", json={"video_id":VIDEO_ONE})
    assert response.status_code == 200
    assert calls[0]["force_retry"] is True


def test_explicit_session_is_copied_into_attempt(setup_download, monkeypatch, tmp_path):
    service, sample = setup_download
    session = tmp_path / 'authorized-cookies.txt'
    session.write_text('# Netscape HTTP Cookie File\n')
    monkeypatch.setattr(youtube_acquisition, 'local_youtube_session', lambda: session)
    calls = fake_extractor(monkeypatch, sample, ['valid'])
    state = await_terminal(service, service.enqueue_download(
        VIDEO_ONE, 'Session test', rights_status='LICENSED', public_verified=True))
    assert state['status'] == 'COMPLETED'
    copied = Path(calls[0]['cookiefile'])
    assert copied.is_relative_to(service.temp_dir)
    assert copied != session
    assert not copied.exists()  # Normal attempt cleanup also removes session copy.
    assert session.read_text() == '# Netscape HTTP Cookie File\n'
    assert 'cookiesfrombrowser' not in calls[0]


def test_expired_session_warning_survives_generic_bot_error(tmp_path, monkeypatch):
    calls = []

    class Downloader:
        def __init__(self, options):
            self.options = options
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def extract_info(self, *_args, **_kwargs):
            self.options['logger'].warning(
                'The provided YouTube account cookies are no longer valid. '
                'They have likely been rotated in the browser as a security measure.')
            raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(youtube_acquisition.yt_dlp, 'YoutubeDL', Downloader)
    monkeypatch.setattr(youtube_acquisition, 'local_youtube_session', lambda: None)
    monkeypatch.setattr(youtube_acquisition, 'po_provider_ready', lambda: True)
    with pytest.raises(youtube_acquisition.AcquisitionFailure) as caught:
        youtube_acquisition.acquire_public_video(
            VIDEO_ONE, tmp_path/'work', tmp_path/'cache', lambda _: False, lambda _: None)
    assert caught.value.status == 'AUTH_REQUIRED'
    assert caught.value.code == 'SESSION_EXPIRED'
    assert len(calls) == 1
    assert caught.value.attempts[0]['code'] == 'SESSION_EXPIRED'
