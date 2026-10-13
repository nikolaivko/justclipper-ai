"""Tests for Gemini error classification, retry policy and fallback chain."""
import pytest
from unittest.mock import Mock, patch, MagicMock
from types import SimpleNamespace


class FakeGeminiError(Exception):
    """Simulates a google.genai API error with a code attribute."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code} {message}")


def make_service(models=None):
    """Create a GeminiService with a mocked client and configurable model chain."""
    from app.services.gemini_service import GeminiService
    svc = GeminiService.__new__(GeminiService)
    svc.api_key = "test"
    svc.model_name = "gemini-3.5-flash-lite"
    svc.client = MagicMock()
    return svc


def basic_settings():
    return {
        "clip_count": 1, "min_clip_seconds": 5, "max_clip_seconds": 60,
        "editorial_guidance": "None", "local_analysis": "None",
        "source_duration": 0,
    }


def valid_response_json():
    import json
    return json.dumps({
        "source_summary": "A test video.",
        "clips": [{
            "rank": 1, "title": "Test Clip", "short_description": "desc",
            "topic": "test", "content_type": "FUNNY", "reason": "good",
            "categories": ["FUNNY"], "context_dependency": "NONE",
            "source_segments": [{"start_seconds": 0, "end_seconds": 5, "reason": "test"}],
        }]
    })


def test_400_invalid_argument_is_not_retried():
    """400 INVALID_ARGUMENT must NOT be retried identically — skip to next model."""
    svc = make_service()
    call_count = {"count": 0}

    def side_effect(**kwargs):
        call_count["count"] += 1
        if kwargs.get("model") == "gemini-3.5-flash-lite":
            raise FakeGeminiError(400, "INVALID_ARGUMENT Request contains an invalid argument.")
        # Second model succeeds
        resp = MagicMock()
        resp.text = valid_response_json()
        return resp

    svc.client.models.generate_content = Mock(side_effect=side_effect)

    with patch("app.services.gemini_service.config") as mock_config:
        mock_config.gemini_models.return_value = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
        mock_config.AUTO_EDIT_DEEP_CANDIDATES = 3
        result = svc.analyze_video(MagicMock(), basic_settings())

    assert result.clips[0].title == "Test Clip"
    # Flash-lite should be called exactly once (no retry), then flash succeeds
    assert call_count["count"] == 2


def test_429_zero_quota_skips_model_immediately():
    """429 with limit: 0 should skip model without retry."""
    svc = make_service()
    models_tried = []

    def side_effect(**kwargs):
        model = kwargs.get("model")
        models_tried.append(model)
        if model == "gemini-3.1-pro-preview":
            raise FakeGeminiError(429,
                "RESOURCE_EXHAUSTED generate_content_free_tier_requests limit: 0")
        resp = MagicMock()
        resp.text = valid_response_json()
        return resp

    svc.client.models.generate_content = Mock(side_effect=side_effect)

    with patch("app.services.gemini_service.config") as mock_config:
        mock_config.gemini_models.return_value = ["gemini-3.1-pro-preview", "gemini-3.5-flash"]
        mock_config.AUTO_EDIT_DEEP_CANDIDATES = 3
        result = svc.analyze_video(MagicMock(), basic_settings())

    assert result.clips[0].title == "Test Clip"
    # Pro-preview tried once (no retry), flash succeeds
    assert [m for m in models_tried if "pro-preview" in m or "flash" in m] == ["gemini-3.1-pro-preview", "gemini-3.5-flash"]


def test_503_is_retried_up_to_3_times():
    """503 UNAVAILABLE should be retried with backoff, then fallback."""
    svc = make_service()
    attempts = {"count": 0}

    def side_effect(**kwargs):
        attempts["count"] += 1
        model = kwargs.get("model")
        if model == "model-a":
            raise FakeGeminiError(503, "UNAVAILABLE This model is currently experiencing high demand.")
        resp = MagicMock()
        resp.text = valid_response_json()
        return resp

    svc.client.models.generate_content = Mock(side_effect=side_effect)

    with patch("app.services.gemini_service.config") as mock_config, \
         patch("app.services.gemini_requests.time") as mock_time:
        mock_config.gemini_models.return_value = ["model-a", "model-b"]
        mock_config.AUTO_EDIT_DEEP_CANDIDATES = 3
        mock_time.sleep = Mock()
        mock_time.monotonic = Mock(return_value=0)
        result = svc.analyze_video(MagicMock(), basic_settings())

    assert result.clips[0].title == "Test Clip"
    # model-a retried 3 times + model-b succeeds = 4
    assert attempts["count"] == 4
    # Sleep only between attempts; no sleep after the exhausted budget.
    assert mock_time.sleep.call_count == 2


def test_all_models_exhausted_gives_user_friendly_message():
    """When all models fail, error message should be user-friendly."""
    svc = make_service()

    svc.client.models.generate_content = Mock(
        side_effect=FakeGeminiError(429, "RESOURCE_EXHAUSTED quota exceeded"))

    with patch("app.services.gemini_service.config") as mock_config:
        mock_config.gemini_models.return_value = ["model-a", "model-b"]
        mock_config.AUTO_EDIT_DEEP_CANDIDATES = 3
        with pytest.raises(RuntimeError) as exc_info:
            svc.analyze_video(MagicMock(), basic_settings())

    assert "no available API quota" in str(exc_info.value)


def test_400_then_503_gives_incompatible_message():
    """When last error is 400, message should mention incompatible request."""
    svc = make_service()

    def side_effect(**kwargs):
        raise FakeGeminiError(400, "INVALID_ARGUMENT bad request")

    svc.client.models.generate_content = Mock(side_effect=side_effect)

    with patch("app.services.gemini_service.config") as mock_config:
        mock_config.gemini_models.return_value = ["model-a"]
        mock_config.AUTO_EDIT_DEEP_CANDIDATES = 3
        with pytest.raises(RuntimeError) as exc_info:
            svc.analyze_video(MagicMock(), basic_settings())

    assert "incompatible" in str(exc_info.value)


def test_no_retry_on_local_validation_errors():
    """Local programming/validation errors are not capacity failures."""
    svc = make_service()
    svc.client.models.generate_content = Mock(side_effect=ValueError("unexpected parse failure"))
    with patch("app.services.gemini_service.config") as mock_config:
        mock_config.gemini_models.return_value = ["model-a", "model-b"]
        with pytest.raises(ValueError, match="unexpected parse failure"):
            svc.analyze_video(MagicMock(), basic_settings())
    assert svc.client.models.generate_content.call_count == 1


def test_default_fallback_chain_excludes_zero_quota_model():
    """Default fallback chain should NOT include gemini-3.1-pro-preview."""
    from app.config import Config
    models = Config.GEMINI_FALLBACK_MODELS
    assert "gemini-3.1-pro-preview" not in models
    assert models == ("gemini-3.8-flash",)


def test_schema_serialization_for_auto_edit():
    """The Auto Edit response schema should serialize to valid JSON under 16KB."""
    import json
    from app.models.schemas import EditorialScores

    interval = {"type": "OBJECT", "properties": {
        "start_seconds": {"type": "NUMBER"}, "end_seconds": {"type": "NUMBER"},
        "reason": {"type": "STRING"}}, "required": ["start_seconds", "end_seconds"]}

    schema = {
        "type": "OBJECT",
        "properties": {
            "source_summary": {"type": "STRING"},
            "clips": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "rank": {"type": "INTEGER"}, "title": {"type": "STRING"},
                "source_segments": {"type": "ARRAY", "items": interval},
                "scores": {"type": "OBJECT", "properties": {
                    name: {"type": "NUMBER"} for name in EditorialScores.model_fields}},
            }, "required": ["rank", "title", "source_segments"]}}
        },
        "required": ["source_summary", "clips"]
    }
    serialized = json.dumps(schema)
    assert len(serialized) < 16384
    # Verify it round-trips
    assert json.loads(serialized) == schema
