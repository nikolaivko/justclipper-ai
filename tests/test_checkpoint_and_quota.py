import pytest
import time
from unittest.mock import Mock, MagicMock, patch
from app.services.gemini_requests import GenerationFailure, GenerationSession, _GLOBAL_QUOTA_UNAVAILABLE_UNTIL, classify_error
from app.services.gemini_service import GeminiService
from app.config import config
from types import SimpleNamespace
from google.genai import types

def make_api_error(code, message):
    class APIError(Exception):
        def __init__(self, c, m):
            self.code = c
            super().__init__(m)
    return APIError(code, message)

@pytest.fixture(autouse=True)
def isolated_quota(monkeypatch):
    _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.clear()
    monkeypatch.setattr("app.services.gemini_requests.time.sleep", Mock())
    yield
    _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.clear()


@pytest.mark.parametrize("resume", [False, True])
def test_source_map_and_broad_checkpoint_resume(resume):
    from app.models.schemas import GeminiAnalysisResponse, ClipCandidate, EDLSegment
    service = GeminiService()
    service._last_generation_model = service.model_name
    service._phase = "broad"
    candidate = ClipCandidate(title="A", short_description="A", topic="A", rank=1,
        content_type="DIALOGUE", reason="A", categories=[], context_dependency="NONE",
        source_segments=[EDLSegment(start_seconds=0, end_seconds=10, reason="story")])
    discovery = GeminiAnalysisResponse(source_summary="A", clips=[candidate])
    saved = {}
    source_map = {"summary": "A", "people": [], "topics": [], "windows": []}
    initial = {"source_map": source_map, "semantic_discovery": discovery.model_dump()} if resume else {}
    def save(stage, data):
        saved.update(data)
    with patch.object(service, "map_source", return_value=source_map) as source, \
         patch.object(service, "_generate_content", return_value=SimpleNamespace(text='{"source_summary":"A","clips":[{"title":"A","short_description":"A","content_type":"DIALOGUE","start_seconds":0,"end_seconds":10}]}')) as generate, \
         patch.object(service, "_deepen_and_critique", side_effect=RuntimeError("interrupted deep")):
        with pytest.raises(RuntimeError, match="interrupted deep"):
            service.analyze_video(types.File(uri="https://example.test/video", mime_type="video/mp4"),
                {"source_duration":60, "auto_edit":True, "count_mode":"TARGET", "clip_count":1},
                checkpoints=initial, on_checkpoint=save)
    assert source.call_count == (0 if resume else 1)
    assert generate.call_count == (0 if resume else 1)
    if not resume:
        assert saved["source_map"] == source_map
        assert saved["semantic_discovery"]["clips"][0]["source_segments"][0]["start_seconds"] == 0

def test_explicit_long_daily_quota_exhaustion_zero_retries():
    _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.clear()
    session = GenerationSession()
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=Mock(side_effect=make_api_error(429, '{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric":"generate_content_requests", "quotaId":"GenerateRequestsPerDay"}]}, {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "85000s"}]}}'))
    ))
    
    start = time.time()
    try:
        session.generate(client, ["a"], [MagicMock(uri=None, mime_type="video/mp4")], phase="broad")
    except Exception:
        pass
    end = time.time()
    
    # 0 immediate retries means it failed fast. Time elapsed should be very small.
    assert end - start < 1.0

def test_exhausted_model_next_compatible_attempted_once():
    _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.clear()
    session = GenerationSession()
    call_counts = {"a": 0, "b": 0}
    
    def side_effect(model, **kwargs):
        call_counts[model] += 1
        if model == "a":
            raise make_api_error(429, '{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric":"generate_content_requests", "quotaId":"GenerateRequestsPerDay"}]}, {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "85000s"}]}}')
        return SimpleNamespace(text='{}')
        
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=side_effect)))
    
    session.generate(client, ["a", "b"], [MagicMock(uri=None, mime_type="video/mp4")], phase="broad")
    assert call_counts["a"] == 1
    assert call_counts["b"] == 1

def test_all_compatible_models_exhausted_raises_quota_exhausted():
    _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.clear()
    session = GenerationSession()
    def side_effect(**kwargs):
        raise make_api_error(429, '{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric":"generate_content_requests", "quotaId":"GenerateRequestsPerDay"}]}, {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "85000s"}]}}')
        
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=side_effect)))
    
    with pytest.raises(GenerationFailure) as excinfo:
        session.generate(client, ["a", "b"], [MagicMock(uri=None, mime_type="video/mp4")], phase="broad")
        
    # The ultimate failure should be QUOTA_EXHAUSTED
    code, category = classify_error(excinfo.value)
    assert getattr(excinfo.value, "error_code", "") == "AI_QUOTA_EXHAUSTED"
    from app.services.gemini_requests import transient_failure
    assert transient_failure(excinfo.value), "Quota must preserve reusable analysis artifacts"

def test_short_transient_429_bounded_retry_works():
    _GLOBAL_QUOTA_UNAVAILABLE_UNTIL.clear()
    session = GenerationSession()
    call_counts = {"a": 0}
    
    def side_effect(model, **kwargs):
        call_counts[model] += 1
        if call_counts[model] < 2:
            raise make_api_error(429, '{"error":{"details":[{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"1.5s"}]}}')
        return SimpleNamespace(text='{}')
        
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=side_effect)))
    
    session.generate(client, ["a"], [MagicMock(uri=None, mime_type="video/mp4")], phase="broad")
    # Retried on the same model.
    assert call_counts["a"] == 2


@pytest.mark.parametrize("metric,quota_id,delay,expected", [
    ("generate_content_input_tokens", "GenerateContentInputTokensPerModelPerMinute-FreeTier", "1.25s", "TOKEN_RATE_LIMIT"),
    ("generate_content_input_tokens", "GenerateContentTokensPerDay", "85711.22542273s", "QUOTA_EXHAUSTED"),
    ("generate_content_requests", "RequestsPerMinute-FreeTier", "2s", "SHORT_RATE_LIMIT"),
    (None, None, "85711.22542273s", "UNKNOWN_RATE_LIMIT"),
])
def test_structured_google_quota_diagnostics(metric, quota_id, delay, expected):
    error = make_api_error(429, "private-provider-message")
    error.details = {"error": {"status":"RESOURCE_EXHAUSTED", "details":[
        {"@type":"type.googleapis.com/google.rpc.QuotaFailure", "violations":[{
            "quotaMetric":metric, "quotaId":quota_id, "quotaValue":"20",
            "quotaDimensions":{"model":"model-a", "location":"global", "private":"do-not-log"}}]},
        {"@type":"type.googleapis.com/google.rpc.RetryInfo", "retryDelay":delay}]}}
    assert classify_error(error) == (429, expected)
    assert error.retry_delay == float(delay[:-1])
    assert error.google_status == "RESOURCE_EXHAUSTED"
    assert error.quota_dimensions.get("private") is None
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=error)))
    session = GenerationSession()
    with pytest.raises(GenerationFailure):
        session.generate(client, ["model-a"], ["text"], phase="critic")
    event = session.diagnostics[0]
    assert event["quota_value"] == ("20" if metric else None)
    assert "private-provider-message" not in repr(session.diagnostics)
    assert "do-not-log" not in repr(session.diagnostics)


def test_supplemental_checkpoint_skips_completed_windows_and_rejects_old_version():
    from app.models.schemas import GeminiAnalysisResponse
    from app.services.gemini_service import SUPPLEMENTAL_CONTEXT_VERSION
    service = GeminiService()
    video = types.File(uri="https://example.test/video", mime_type="video/mp4")
    result = GeminiAnalysisResponse(source_summary="A", clips=[])
    checkpoint = {"version":SUPPLEMENTAL_CONTEXT_VERSION, "result":result.model_dump(), "windows":[[0,300],[300,600]]}
    with patch.object(service, "_generate_content", return_value=SimpleNamespace(text='{"source_summary":"A","clips":[]}')) as request:
        service._run_target_discovery_passes(video, {"count_mode":"TARGET", "clip_count":20, "source_duration":1812.8}, result, {}, "model", checkpoint=checkpoint)
    assert request.call_count == 1
    assert request.call_args.kwargs["contents"][0].video_metadata.start_offset == "600.000s"
    checkpoint["version"] = -1
    with patch.object(service, "_generate_content", return_value=SimpleNamespace(text='{"source_summary":"A","clips":[]}')) as request:
        service._run_target_discovery_passes(video, {"count_mode":"TARGET", "clip_count":20, "source_duration":1812.8}, result, {}, "model", checkpoint=checkpoint)
    assert request.call_count == 3
    assert request.call_args_list[0].kwargs["contents"][0].video_metadata.start_offset == "0.000s"


def test_discovery_assigns_unique_ranks_without_inventing_payoff():
    import json
    service = GeminiService()
    service._generation_session = GenerationSession()
    service._phase = "broad"
    service._last_generation_model = "model"
    candidate = {"title":"A", "short_description":"A", "content_type":"DIALOGUE",
                 "start_seconds":620, "end_seconds":650, "payoff_present":True}
    result = service._parse_analysis(SimpleNamespace(text=json.dumps({"source_summary":"A", "clips":[candidate,candidate]})), 1812.8, is_discovery=True)
    assert [c.rank for c in result.clips] == [1,2]
    assert all(c.payoff_timestamp is None for c in result.clips)
