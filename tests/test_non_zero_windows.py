import pytest
from unittest.mock import Mock, patch, MagicMock
from app.services.gemini_service import GeminiService
from types import SimpleNamespace
from google.genai import types

def test_non_zero_window_prompt_contains_explicit_bounds():
    service = GeminiService()
    
    prompts = []
    prompt_captured = None
    def fake_generate_content(*args, **kwargs):
        nonlocal prompt_captured
        contents = kwargs.get("contents", [])
        if contents:
            prompt_captured = contents[1] if len(contents) > 1 else contents[0]
            prompts.append(prompt_captured)
        mock_response = MagicMock()
        mock_response.text = '{"source_summary":"test", "clips":[]}'
        return mock_response

    with patch.object(service, "_generate_content", side_effect=fake_generate_content):
        video_file = MagicMock()
        video_file.uri = "gs://fake/video"
        video_file.mime_type = "video/mp4"
        
        from app.models.schemas import GeminiAnalysisResponse, ClipCandidate, EDLSegment
        initial_result = GeminiAnalysisResponse(source_summary="test", clips=[
            ClipCandidate(title="c1", rank=1, short_description="d", content_type="t", topic="t", reason="", categories=[], context_dependency="NONE", source_segments=[EDLSegment(start_seconds=300, end_seconds=600, reason="")]),
            ClipCandidate(title="c2", rank=2, short_description="d", content_type="t", topic="t", reason="", categories=[], context_dependency="NONE", source_segments=[EDLSegment(start_seconds=840, end_seconds=1200, reason="")])
        ])
        
        settings = {"count_mode": "TARGET", "clip_count": 5, "source_duration": 1812.8}
        service._run_target_discovery_passes(video_file, settings, initial_result, {}, "gemini-3.5-flash-lite")
        
        prompt_captured = prompts[0]
        assert prompt_captured is not None
        assert "TIMESTAMP_COORDINATE_SYSTEM = ABSOLUTE_SOURCE_TIME_SECONDS" in prompt_captured
        assert "FULL_SOURCE_DURATION_SECONDS = 1812.8" in prompt_captured
        assert "WINDOW_START_SOURCE_SECONDS = 1200.0" in prompt_captured
        assert "WINDOW_END_SOURCE_SECONDS = 1500.0" in prompt_captured
        assert "ABSOLUTE SECONDS" in prompt_captured

def test_deep_pass_window_bounds_enforced():
    service = GeminiService()
    prompt_captured = None
    
    def fake_generate_content(*args, **kwargs):
        nonlocal prompt_captured
        if kwargs.get("phase") == "deep":
            contents = kwargs.get("contents", [])
            prompt_captured = contents[1] if len(contents) > 1 else contents[0]
        mock_response = MagicMock()
        mock_response.text = '{"title": "a", "short_description": "a", "topic": "a", "content_type": "DIALOGUE", "story_arc": null, "source_segments": [], "removed_regions": [], "required_context": [], "preserve_pauses": [], "reaction_moments": [], "payoff_present": false, "payoff_timestamp": null, "resolution_present": false, "ending_quality": "a", "intentional_unresolved_ending": false, "hook_strategy": "a", "pacing_mode": "a", "caption_emphasis": [], "caption_motion": "a", "scores": null, "micro_beats": [], "emotional_arc": [], "dominant_emotion": "a", "peak_moment": null, "reaction_window": null, "importance_curve": [], "hook_variants": [], "natural_loop_score": 0.0, "effect_events": [], "first_frame_reason": "a", "ending_reason": "a", "cut_continuity": [], "visual_events": [], "protected_regions": [], "context_overlay": null}'
        return mock_response
        
    from app.models.schemas import ClipCandidate, EDLSegment, GeminiAnalysisResponse
    candidate = ClipCandidate(title="c1", rank=1, short_description="d", content_type="t", topic="t", reason="", categories=[], context_dependency="NONE", source_segments=[EDLSegment(start_seconds=100.0, end_seconds=160.0, reason="")])
    
    with patch.object(service, "_generate_content", side_effect=fake_generate_content), \
         patch.object(service, "_parse_analysis", return_value=GeminiAnalysisResponse(source_summary="a", clips=[candidate])):
        video_file = MagicMock()
        video_file.uri = "gs://fake/video"
        video_file.mime_type = "video/mp4" # NEED MIME TYPE!
        
        service._deepen_and_critique(video_file, GeminiAnalysisResponse(source_summary="a", clips=[candidate]), {}, "gemini-3.5-flash-lite", 1800.0)
        
        assert prompt_captured is not None
        assert "FULL_SOURCE_DURATION_SECONDS = 1800.0" in prompt_captured
        assert "WINDOW_START_SOURCE_SECONDS = 98.0" in prompt_captured
        assert "WINDOW_END_SOURCE_SECONDS = 162.0" in prompt_captured


def test_non_zero_window_rejects_relative_coordinates():
    from app.models.schemas import ClipCandidate, EDLSegment, GeminiAnalysisResponse
    from app.services.semantic_validation import timeline_audit
    candidate = ClipCandidate(title="moment", rank=1, short_description="d", topic="t",
        content_type="t", reason="", categories=[], context_dependency="NONE",
        source_segments=[EDLSegment(start_seconds=10, end_seconds=50, reason="")])
    result = GeminiAnalysisResponse(source_summary="test", clips=[candidate])
    assert not timeline_audit(result, 1812.8, window=(620, 940))[0]["valid"]
    candidate.source_segments = [EDLSegment(start_seconds=630, end_seconds=670, reason="")]
    assert timeline_audit(result, 1812.8, window=(620, 940))[0]["valid"]
    candidate.peak_moment = 25
    assert "EVENT_OUT_OF_WINDOW" in timeline_audit(result, 1812.8, window=(620, 940))[0]["reasons"]
