import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.schemas import GeminiAnalysisResponse
from app.services.gemini_service import GeminiService
from app.services.gemini_requests import GenerationSession
from app.services.semantic_validation import InvalidSemanticTimeline, timeline_audit
from app.services.editorial_service import prepare_candidates


def response(start=1855, end=1940):
    return {"source_summary": "A challenge", "clips": [{
        "rank": 1, "title": "Pool shot", "short_description": "A shot lands",
        "topic": "Pool", "reason": "Payoff", "categories": ["CHALLENGE"],
        "content_type": "challenge", "context_dependency": "low",
        "source_segments": [{"start_seconds": start, "end_seconds": end}],
        "scores": {"hook_strength": 8, "standalone_clarity": 9, "payoff_strength": 9},
        "payoff_present": True, "payoff_timestamp": end - 2}]}


def service_with(*results):
    service = GeminiService()
    generate = Mock(side_effect=[SimpleNamespace(text=json.dumps(r)) for r in results])
    service.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate), files=Mock())
    service._deepen_and_critique = Mock()
    return service, generate


def test_out_of_source_is_not_quality_rejection_and_repair_is_bounded():
    service, generate = service_with(response(), response())
    events = []
    service.on_request_event = events.append
    with pytest.raises(InvalidSemanticTimeline) as exc:
        service.analyze_video(object(), {"source_duration": 1822.58068})
    assert str(exc.value) == "AI_INVALID_TIMELINE"
    assert generate.call_count == 2
    service.client.files.upload.assert_not_called()
    service._deepen_and_critique.assert_not_called()
    assert [e["phase"] for e in events if e["category"] == "SUCCESS"] == ["broad", "broad_timeline_repair"]
    assert "SOURCE_SEGMENT_OUT_OF_BOUNDS" in exc.value.audit[0]["reasons"]


def test_repair_uses_same_video_and_explicit_seconds_without_lowering_scores():
    service, generate = service_with(response(), response(1135, 1160))
    video = object()
    result = service.analyze_video(video, {"source_duration": 1822.58068})
    assert result.clips[0].source_segments[0].start_seconds == 1135
    assert result.clips[0].scores.standalone_clarity == 9
    assert all(call.kwargs["contents"][0] is video for call in generate.call_args_list)
    assert "1822.58068" in generate.call_args_list[0].kwargs["contents"][1]
    assert "SOURCE_SEGMENT_OUT_OF_BOUNDS" in generate.call_args_list[1].kwargs["contents"][1]
    assert "analysis_models" in result.model_dump(exclude_unset=True)


def test_real_shape_has_high_scores_but_invalid_timeline():
    result = GeminiAnalysisResponse.model_validate(response())
    assert not timeline_audit(result, 1822.58068)[0]["valid"]
    decisions = []
    assert prepare_candidates(result.clips, 1822.58068, decisions=decisions) == []
    assert decisions[0]["decision"] == "NO_VALID_SEGMENTS"
    assert decisions[0]["editorial_score"] is None
    assert decisions[0]["scores"]["standalone_clarity"] == 9


def test_rejection_reasons_keep_candidate_identity_and_quality_floor():
    data = response(20, 45)
    second = response(70, 95)["clips"][0]
    second["rank"] = 2
    second["scores"] = {"hook_strength": 1, "standalone_clarity": 1, "payoff_strength": 1}
    data["clips"].append(second)
    result = GeminiAnalysisResponse.model_validate(data)
    decisions = []
    chosen = prepare_candidates(result.clips, 120, decisions=decisions)
    assert len(chosen) == 2
    assert [(d["rank"], d["decision"]) for d in decisions] == [(1, "SELECTED"), (2, "SELECTED")]
    assert "BELOW_MIN_SCORE" in chosen[1].quality_flags


def test_retry_after_503_is_observable(monkeypatch):
    class Unavailable(Exception):
        code = 503
    monkeypatch.setattr("app.services.gemini_requests.time.sleep", lambda n: None)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(
        side_effect=[Unavailable(), SimpleNamespace(text="{}")])))
    events = []
    GenerationSession(events.append).generate(client, ["model"], "prompt", phase="broad")
    assert [(e["http_status"], e["attempt"]) for e in events] == [(503, 1), (200, 2)]


def test_truthful_ai_progress_stages():
    from app.services.progress_service import progress_tracker
    job_id = "timeline-stage-test"
    progress_tracker.start_job(job_id, 100, "ANALYSIS")
    values = []
    for stage in ["UPLOADING_TO_AI", "WAITING_FOR_AI_FILE", "ANALYZING_WITH_AI",
                  "BUILDING_CANDIDATES", "VALIDATING_CANDIDATES"]:
        progress_tracker.update_stage(job_id, stage, stage)
        progress = progress_tracker.get_job_progress(job_id)
        assert progress["stage"] == stage
        assert progress["stage_progress"] == 0
        values.append(progress["overall_progress"])
    assert values == sorted(set(values))
    progress_tracker.active_jobs.pop(job_id)


def test_invalid_cache_retry_reuses_bound_local_analysis_and_valid_proxy(tmp_path, monkeypatch):
    from app.config import config
    from app.services.clip_service import ClipService
    from app.services.ffmpeg_service import FFmpegService
    from app.services.file_service import FileService
    from app.services.job_store import job_store
    from app.services.semantic_cache import SemanticCache
    from app.services.worker import PipelineWorker
    monkeypatch.setattr(config, "DATA_CACHE", tmp_path / "cache")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same source bytes")
    proxy = tmp_path / "proxy.mp4"
    proxy.write_bytes(b"valid existing proxy")
    monkeypatch.setattr(FileService, "get_proxy_path", lambda *a: proxy)
    job = ClipService.create_job("invalid-timeline", source.name, source)
    job.update(metadata={"duration": 1822.58068, "file_size": 60 * 1024**2},
               analysis_settings={"clip_count": 1}, preserve_analysis_artifacts=True)
    cache = SemanticCache()
    key = cache.key(source, job["analysis_settings"])
    local = {"silences": [], "scenes": [10, 20], "speech": []}
    cache.save(key, GeminiAnalysisResponse.model_validate(response()), local)
    original = (cache.root / f"{key}.json").read_bytes()
    job["proxy_source_cache_key"] = key + "-timeline-v1"
    job_store.set(job["id"], job)
    forbidden = Mock(side_effect=AssertionError("valid artifact regenerated"))
    monkeypatch.setattr(FFmpegService, "create_proxy", forbidden)
    monkeypatch.setattr(ClipService, "run_local_analysis", forbidden)
    monkeypatch.setattr(FFmpegService, "get_metadata", lambda p: SimpleNamespace(duration=1822.58068))
    upload = Mock(return_value=SimpleNamespace(name="new-remote"))
    monkeypatch.setattr(GeminiService, "upload_video", upload)
    monkeypatch.setattr(GeminiService, "delete_video", Mock())
    monkeypatch.setattr(GeminiService, "analyze_video", lambda *a: GeminiAnalysisResponse.model_validate(response(20, 45)))
    PipelineWorker()._run_analysis(job)
    saved = job_store.get(job["id"])
    assert saved["status"] == "READY_TO_RENDER"
    assert saved["analysis_data"] == local
    assert saved["selection_funnel"]["final"] == 1
    assert saved["candidate_decisions"][0]["decision"] == "SELECTED"
    assert (cache.root / f"{key}.json").read_bytes() == original
    assert proxy.read_bytes() == b"valid existing proxy"
    upload.assert_called_once()


def test_invalid_semantics_error_survives_worker_classification(tmp_path, monkeypatch):
    from app.services.clip_service import ClipService
    from app.services.job_store import job_store
    from app.services.worker import PipelineWorker
    monkeypatch.setattr(job_store, "jobs_dir", tmp_path)
    job = ClipService.create_job("bad-times", "source", tmp_path / "source")
    job["lifecycle_version"] = 0
    worker = PipelineWorker()
    monkeypatch.setattr(worker, "_run_analysis", Mock(side_effect=InvalidSemanticTimeline([])))
    worker._process_analysis(job)
    assert job_store.get(job["id"])["error"] == "AI_INVALID_TIMELINE"


def test_upload_reports_waiting_before_file_poll(tmp_path, monkeypatch):
    service = GeminiService()
    stages = []
    service.on_stage = lambda stage, message: stages.append(stage)
    service.client = SimpleNamespace(files=SimpleNamespace(
        upload=Mock(return_value=SimpleNamespace(name="file", state="PROCESSING")),
        get=Mock(return_value=SimpleNamespace(name="file", state="ACTIVE"))))
    monkeypatch.setattr("time.sleep", lambda n: None)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    assert service.upload_video(video).state == "ACTIVE"
    assert stages == ["WAITING_FOR_AI_FILE"]
