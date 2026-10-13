import pytest
from unittest.mock import patch, MagicMock
from app.services.gemini_service import GeminiService
from app.models.schemas import GeminiAnalysisResponse, ClipCandidate, EDLSegment, GeminiDiscoveryResponse
from google.genai import types
import json

def test_supplemental_window_metadata():
    service = GeminiService()
    video_file = MagicMock()
    video_file.uri = "gs://fake/video"
    video_file.mime_type = "video/mp4"

    initial_result = GeminiAnalysisResponse(source_summary="test", clips=[])

    with patch.object(service, "_generate_content") as mock_gen:
        mock_response = MagicMock()
        mock_response.text = '{"source_summary":"test", "clips":[]}'
        mock_gen.return_value = mock_response

        settings = {"count_mode": "TARGET", "clip_count": 5, "source_duration": 1812.8, "min_clip_seconds": 30, "max_clip_seconds": 60}
        schema = {"type": "OBJECT"}
        
        service._run_target_discovery_passes(video_file, settings, initial_result, schema, "gemini-3.5-flash-lite")
        
        assert mock_gen.call_count >= 1
        args, kwargs = mock_gen.call_args_list[0]
        contents = kwargs.get("contents", args[2] if len(args) > 2 else None)
        assert contents is not None
        focused_video = contents[0]
        
        assert isinstance(focused_video, types.Part)
        assert focused_video.video_metadata is not None
        assert focused_video.video_metadata.start_offset == "0.000s"
        assert focused_video.video_metadata.end_offset == "300.000s"

def test_context_budget_target10():
    total_seconds = 0
    windows = []
    def fake_generate_content(*args, **kwargs):
        nonlocal total_seconds
        contents = kwargs.get("contents", [])
        if contents and isinstance(contents[0], types.Part) and contents[0].video_metadata:
            start = float(contents[0].video_metadata.start_offset[:-1])
            end = float(contents[0].video_metadata.end_offset[:-1])
            windows.append((start, end))
            total_seconds += (end - start)
        elif contents and not isinstance(contents[0], types.Part):
            total_seconds += 1812.8
        mock_response = MagicMock()
        mock_response.text = '{"source_summary":"test", "clips":[]}'
        return mock_response

    service = GeminiService()
    with patch.object(service, "_generate_content", side_effect=fake_generate_content):
        initial_result = GeminiAnalysisResponse(source_summary="test", clips=[
            ClipCandidate(title="c1", rank=1, short_description="d", content_type="t", topic="t", reason="", categories=[], context_dependency="NONE", source_segments=[EDLSegment(start_seconds=300, end_seconds=600, reason="")]),
            ClipCandidate(title="c2", rank=2, short_description="d", content_type="t", topic="t", reason="", categories=[], context_dependency="NONE", source_segments=[EDLSegment(start_seconds=840, end_seconds=1200, reason="")]),
            ClipCandidate(title="c3", rank=3, short_description="d", content_type="t", topic="t", reason="", categories=[], context_dependency="NONE", source_segments=[EDLSegment(start_seconds=1380, end_seconds=1812.8, reason="")])
        ])
        video_file = MagicMock()
        video_file.uri = "gs://fake/video"
        video_file.mime_type = "video/mp4"
        
        settings = {"count_mode": "TARGET", "clip_count": 10, "source_duration": 1812.8}
        service._run_target_discovery_passes(video_file, settings, initial_result, {}, "gemini-3.5-flash-lite")
        
        expected = [(0.0, 300.0), (600.0, 840.0), (1200.0, 1380.0)]
        assert windows == expected
        assert total_seconds == sum(end - start for start, end in expected)
