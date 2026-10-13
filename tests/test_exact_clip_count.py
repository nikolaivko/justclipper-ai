import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.routes import analyze_video, render_clips
from app.config import config
from app.models.schemas import (ClipCandidate, EDLSegment, EditorialScores,
                                GeminiAnalysisResponse, RenderJobRequest,
                                RenderSettings)
from app.services.clip_service import ClipService
from app.services.count_fill import fill_requested_count
from app.services.gemini_service import GeminiService, PROMPT_VERSION
from app.services.job_store import job_store
from app.services.semantic_cache import SemanticCache
from app.services.worker import PipelineWorker


def candidate(start=10, end=25, rank=1):
    return ClipCandidate(
        rank=rank, title="Verified real moment", short_description="A source moment.",
        topic="source", content_type="VIDEO", reason="complete moment",
        categories=["STORY"], context_dependency="LOW",
        source_segments=[EDLSegment(start_seconds=start, end_seconds=end)],
        payoff_present=True, payoff_timestamp=max(start, end - 1),
        scores=EditorialScores(first_1s_strength=8, first_3s_strength=8,
                               standalone_clarity=8, payoff_strength=9,
                               ending_strength=8),
        overall_score=8, editorial_score=80,
    )


def test_exact_count_fills_uncovered_timeline_without_overlap():
    result = fill_requested_count([candidate()], 15, 1200, 10, 30,
                                  {"speech": [{"start": 0, "end": 1200}]})
    assert len(result) == 15
    assert result[0].selection_origin == "GEMINI"
    assert sum(item.selection_origin == "TIMELINE_FILL" for item in result) == 14
    spans = sorted((s.start_seconds, s.end_seconds) for item in result
                   for s in item.source_segments)
    assert all(end - start >= 10 for start, end in spans)
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:]))
    assert all(item.overall_score == 0 for item in result[1:])
    assert all("Review" in item.short_description for item in result[1:])


def test_exact_count_cannot_invent_more_source_time():
    assert len(fill_requested_count([], 15, 90, 10, 30)) < 15
    assert len(fill_requested_count([], 15, 150, 10, 30)) == 15


def test_exact_analysis_uses_bounded_timeline_passes():
    service = GeminiService()
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            clips = [candidate(10, 25)]
        elif len(calls) == 2:
            clips = [candidate(30, 45)]
        elif len(calls) == 3:
            clips = [candidate(220, 235)]
        else:
            clips = [candidate(10, 25)]  # Outside the last window; discard.
        return SimpleNamespace(text=GeminiAnalysisResponse(
            source_summary="source", clips=clips).model_dump_json())

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content))
    result = service.analyze_video(object(), {
        "clip_count": 15, "exact_clip_count": True, "source_duration": 600,
        "min_clip_seconds": 10, "max_clip_seconds": 30,
    })
    assert len(calls) == 1
    assert len(result.clips) == 1


def test_legacy_gemini_setting_uses_current_model_for_analysis():
    service = GeminiService()
    service.model_name = "gemini-legacy-model"
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs["model"])
        return SimpleNamespace(text=GeminiAnalysisResponse(
            source_summary="source", clips=[candidate()]).model_dump_json())

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content))
    service.analyze_video(object(), {"clip_count": 1, "exact_clip_count": True})
    from app.config import config
    assert calls[0] == config.GEMINI_MODEL


def test_cached_exact_analysis_produces_fifteen_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_CACHE", tmp_path / "cache")
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    job = ClipService.create_job("exactjob", "source.mp4", source)
    job["metadata"] = {"duration": 1200.0, "file_size": source.stat().st_size}
    job["analysis_settings"] = {
        "clip_count": 15, "exact_clip_count": True,
        "min_clip_seconds": 10, "max_clip_seconds": 30,
    }
    job_store.set(job["id"], job)
    cache = SemanticCache()
    cache.save(cache.key(source, job["analysis_settings"]), GeminiAnalysisResponse(source_summary="source", clips=[candidate()], analysis_version=PROMPT_VERSION), {"silences": [], "scenes": [], "speech": [{"start": 0, "end": 1200}]}, 15)
    monkeypatch.setattr(GeminiService, "upload_video", lambda *a, **kw: pytest.fail("cache miss"))
    PipelineWorker()._run_analysis(job)
    saved = job_store.get(job["id"])
    assert saved["status"] == "READY_TO_RENDER"
    assert len(saved["candidates"]) == 15
    assert sum(c["selection_origin"] == "TIMELINE_FILL" for c in saved["candidates"]) == 14


def test_exact_count_uses_labelled_source_excerpts_if_gemini_unavailable(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_CACHE", tmp_path / "cache")
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    job = ClipService.create_job("fallbackjob", "source.mp4", source)
    job["metadata"] = {"duration": 1200.0, "file_size": source.stat().st_size}
    job["analysis_settings"] = {
        "clip_count": 15, "exact_clip_count": True,
        "min_clip_seconds": 10, "max_clip_seconds": 30,
    }
    job_store.set(job["id"], job)

    def local_analysis(job_id, _path, _duration):
        saved = job_store.get(job_id)
        saved["analysis_data"] = {"silences": [], "scenes": [], "speech": []}
        job_store.set(job_id, saved)

    monkeypatch.setattr(ClipService, "run_local_analysis", local_analysis)
    monkeypatch.setattr(GeminiService, "upload_video", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")))
    PipelineWorker()._run_analysis(job)
    saved = job_store.get(job["id"])
    assert saved["status"] == "READY_TO_RENDER"
    assert len(saved["candidates"]) == 15
    assert all(c["selection_origin"] == "TIMELINE_FILL" for c in saved["candidates"])
    assert "AI analysis was unavailable" in saved["source_summary"]
    assert SemanticCache().load(SemanticCache().key(source, job["analysis_settings"])) is None


def test_exact_count_rejects_source_too_short_before_processing(monkeypatch):
    monkeypatch.setattr(ClipService, "get_job", lambda *a, **kw: {
        "metadata": {"duration": 90}, "status": "IDLE"})
    settings = RenderSettings(clip_count=15, exact_clip_count=True,
                              min_clip_seconds=10, max_clip_seconds=30)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(analyze_video("short", BackgroundTasks(), settings))
    assert raised.value.detail == "SOURCE_TOO_SHORT_FOR_TARGET"


def test_partial_exact_render_keeps_source_and_reuses_valid_mp4(
        tmp_path, monkeypatch):
    import subprocess
    from app.services.ffmpeg_service import FFmpegService
    from app.services.storage_lifecycle import storage_lifecycle
    from app.services.video_analyzer import LocalVideoAnalyzer
    from app.services.visual_director import VisualDirector

    output = tmp_path / "output"
    output.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=10",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "0.4", "-c:v", "mpeg4", "-c:a", "aac",
        "-shortest", str(source)], check=True, capture_output=True)
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_CACHE", tmp_path / "cache")
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    monkeypatch.setattr(storage_lifecycle, "root", tmp_path)
    monkeypatch.setattr(LocalVideoAnalyzer, "track_faces", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(LocalVideoAnalyzer, "detect_scenes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(LocalVideoAnalyzer, "detect_speech", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(VisualDirector, "build_shot_plan", lambda *_args, **_kwargs: [])

    job = ClipService.create_job("partialexact", "source.mp4", source)
    job["source_cleanup_eligible"] = True
    job_store.set(job["id"], job)
    clips = [candidate(0, .3, 1), candidate(0, .3, 2)]
    settings = RenderSettings(clip_count=2, exact_clip_count=True,
                              min_clip_seconds=0, max_clip_seconds=1)
    calls = []

    def first_render(source, dest, **_kwargs):
        calls.append(dest)
        if "clip_02" in dest.name:
            return False
        dest.write_bytes(source.read_bytes())
        return True

    monkeypatch.setattr(FFmpegService, "render_clip", first_render)
    # This fixture copies a 32x32 source instead of producing a vertical Short;
    # the test exercises partial-render reuse, not the real render QC gate.
    monkeypatch.setattr("app.services.render_qc.check_render",
                        lambda *_args, **_kwargs: None)
    ClipService.process_renders(job["id"], clips, settings)
    saved = job_store.get(job["id"])
    assert saved["status"] == "FAILED"
    assert saved["error"] == "INCOMPLETE_RENDER_COUNT"
    assert [render["success"] for render in saved["renders"]] == [True, False]
    assert source.exists()
    successful_path = Path(saved["renders"][0]["output_path"])
    assert successful_path.exists()

    calls.clear()

    def retry_render(source, dest, **_kwargs):
        calls.append(dest)
        dest.write_bytes(source.read_bytes())
        return True

    monkeypatch.setattr(FFmpegService, "render_clip", retry_render)
    ClipService.process_renders(job["id"], clips, settings)
    saved = job_store.get(job["id"])
    assert saved["status"] == "COMPLETED"
    assert len(calls) == 1 and "clip_02" in calls[0].name
    assert all(render["success"] for render in saved["renders"])


def test_direct_upload_render_stops_without_deleting_source_on_low_space(tmp_path, monkeypatch):
    from app.services.storage_lifecycle import storage_lifecycle

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    job = ClipService.create_job("lowspace", "source.mp4", source)
    job["storage_profile"] = "DIRECT_UPLOAD"
    job["source_cleanup_eligible"] = True
    job_store.set(job["id"], job)
    monkeypatch.setattr(storage_lifecycle, "get_disk_usage", lambda: {
        "total": 5 * 1024 ** 3, "used": 4 * 1024 ** 3,
        "free": int(config.DIRECT_UPLOAD_MIN_FREE_GB * 1024 ** 3) + 64 * 1024 ** 2 - 1})
    ClipService.process_renders(job["id"], [candidate()], RenderSettings())
    saved = job_store.get(job["id"])
    assert saved["status"] == "FAILED"
    assert saved["error"] == "DISK_FULL"
    assert saved["renders"][0]["error"] == "DISK_FULL"
    assert source.exists()


def test_render_route_enforces_exact_only_for_full_selection(tmp_path, monkeypatch):
    from app.services.heavy_gate import heavy_gate
    from app.services.storage_lifecycle import storage_lifecycle

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    monkeypatch.setattr(storage_lifecycle, "require_space", lambda: None)
    monkeypatch.setattr(heavy_gate, "claim", lambda *a, **kw: None)
    monkeypatch.setattr(heavy_gate, "release", lambda *a, **kw: None)
    job = ClipService.create_job("routeexact", "source.mp4", source)
    job["metadata"] = {"duration": 1200.0}
    job["analysis_settings"] = {"clip_count": 2, "exact_clip_count": True}
    job["candidates"] = [candidate(10, 25, 1).model_dump(),
                         candidate(30, 45, 2).model_dump()]
    job_store.set(job["id"], job)
    full = RenderJobRequest(filename="source.mp4", candidates=[
        candidate(10, 25, 1), candidate(30, 45, 2)], settings=RenderSettings())
    asyncio.run(render_clips(job["id"], full, BackgroundTasks()))
    saved = job_store.get(job["id"])
    assert saved["render_request"]["settings"]["exact_clip_count"] is True
    assert saved["render_request"]["settings"]["clip_count"] == 2

    saved["status"] = "READY_TO_RENDER"
    job_store.set(job["id"], saved)
    partial = RenderJobRequest(filename="source.mp4", candidates=[candidate(10, 25, 1)],
                               settings=RenderSettings())
    asyncio.run(render_clips(job["id"], partial, BackgroundTasks()))
    assert job_store.get(job["id"])["render_request"]["settings"]["exact_clip_count"] is False

    saved = job_store.get(job["id"])
    saved["status"] = "READY_TO_RENDER"
    edited = candidate(10, 25, 1).model_dump()
    edited["title"] = "Reviewed opening"
    saved["render_request"]["candidates"] = [edited, candidate(30, 45, 2).model_dump()]
    job_store.set(job["id"], saved)
    retry = RenderJobRequest(filename="source.mp4", candidates=[], settings=RenderSettings())
    asyncio.run(render_clips(job["id"], retry, BackgroundTasks()))
    retried = job_store.get(job["id"])["render_request"]
    assert retried["candidates"][0]["title"] == "Reviewed opening"
    assert retried["settings"]["exact_clip_count"] is True

    saved = job_store.get(job["id"])
    saved["status"] = "READY_TO_RENDER"
    job_store.set(job["id"], saved)
    duplicate = RenderJobRequest(filename="source.mp4", candidates=[
        candidate(10, 25, 1), candidate(30, 45, 1)], settings=RenderSettings())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(render_clips(job["id"], duplicate, BackgroundTasks()))
    assert raised.value.detail == "DUPLICATE_CLIP_RANK"


def test_corrupt_saved_retry_releases_processing_gate(tmp_path, monkeypatch):
    from app.api import routes
    from unittest.mock import Mock
    monkeypatch.setattr(job_store, 'jobs_dir', tmp_path)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture')
    job_store.set('broken', {'id': 'broken', 'status': 'FAILED', 'filepath': str(source),
                            'candidates': [{'rank': 1, 'source_segments': 'invalid'}]})
    monkeypatch.setattr(routes.storage_lifecycle, 'require_processing_space', Mock())
    monkeypatch.setattr(routes.heavy_gate, 'claim', Mock())
    monkeypatch.setattr(routes.heavy_gate, 'release', Mock())
    with pytest.raises(HTTPException) as error:
        asyncio.run(render_clips('broken', RenderJobRequest(filename='source.mp4', candidates=[], settings=RenderSettings()), BackgroundTasks()))
    assert error.value.status_code == 422
    routes.heavy_gate.release.assert_called_once_with('broken')
