import pytest
from app.services.timeline import remap_intervals

def test_long_candidate_compression_remapping_captions():
    # 8. caption timeline remapping across removed gap
    # Original candidate had two pieces after compression:
    # 10-20 (10s) and 30-45 (15s) -> total 25s
    shot_plan = [
        {"start": 10, "end": 20},
        {"start": 30, "end": 45}
    ]
    subtitles = [
        {"start_seconds": 12, "end_seconds": 15, "text": "Hello"}, # Inside first gap: maps to 2-5
        {"start_seconds": 25, "end_seconds": 28, "text": "Lost"},  # In removed gap: should disappear or be cropped? Wait, remap_intervals drops it entirely if it doesn't overlap!
        {"start_seconds": 35, "end_seconds": 40, "text": "World"}  # Inside second gap: maps to 10 + (35-30) = 15, end = 10 + (40-30) = 20
    ]
    
    mapped = remap_intervals(shot_plan, subtitles)
    assert len(mapped) == 2
    assert mapped[0]["text"] == "Hello"
    assert mapped[0]["start_seconds"] == 2
    assert mapped[0]["end_seconds"] == 5
    
    assert mapped[1]["text"] == "World"
    assert mapped[1]["start_seconds"] == 15
    assert mapped[1]["end_seconds"] == 20

def test_long_candidate_compression_remapping_visual_events():
    # 9. visual event timeline remapping across removed gap
    shot_plan = [
        {"start": 50, "end": 60},
        {"start": 100, "end": 115}
    ]
    events = [
        {"start_seconds": 55, "end_seconds": 65, "reason": "Overlap cut"}, # Maps to 5-10
        {"start_seconds": 105, "end_seconds": 110, "reason": "Inside"} # Maps to 10 + 5 = 15-20
    ]
    mapped = remap_intervals(shot_plan, events)
    assert len(mapped) == 2
    assert mapped[0]["start_seconds"] == 5
    assert mapped[0]["end_seconds"] == 10
    
    assert mapped[1]["start_seconds"] == 15
    assert mapped[1]["end_seconds"] == 20

from unittest.mock import MagicMock
from app.services.auto_edit_director import AutoEditDirector
from app.models.schemas import ClipCandidate, EDLSegment, GeminiAnalysisResponse

def make_candidate(start, end):
    return ClipCandidate(
        rank=1, title="Test", short_description="Test", topic="T", content_type="VLOG",
        reason="R", categories=[], context_dependency="LOW",
        source_segments=[EDLSegment(start_seconds=start, end_seconds=end)],
        payoff_present=True, payoff_timestamp=start+5
    )

def test_candidate_compression_survives():
    # 1. semantic candidate = 142 sec, repair EDL = 51 sec -> survives
    service = MagicMock()
    # Mock analyze_video to return one long candidate
    service.analyze_video.return_value = GeminiAnalysisResponse(
        source_summary="Test", clips=[make_candidate(0, 142)], analysis_version="v"
    )
    # Mock compress_candidate to return a valid 51s segment list
    service.compress_candidate.return_value = [EDLSegment(start_seconds=0, end_seconds=51)]
    
    result = AutoEditDirector.understand(service, "file", {"max_clip_seconds": 60, "min_clip_seconds": 5})
    assert result.clips[0].compression_attempted is True
    assert result.clips[0].compression_status == "SUCCESS"
    assert sum(s.end_seconds - s.start_seconds for s in result.clips[0].source_segments) == 51

def test_candidate_already_45_sec_no_repair():
    # 6. candidate already 45 sec -> no repair invoked
    service = MagicMock()
    service.analyze_video.return_value = GeminiAnalysisResponse(
        source_summary="Test", clips=[make_candidate(0, 45)], analysis_version="v"
    )
    result = AutoEditDirector.understand(service, "file", {"max_clip_seconds": 60, "min_clip_seconds": 5})
    assert getattr(result.clips[0], "compression_attempted", False) is False

def test_long_candidate_repair_still_too_long():
    # 3. long candidate repair still >60 sec -> hard rejected
    service = MagicMock()
    service.analyze_video.return_value = GeminiAnalysisResponse(
        source_summary="Test", clips=[make_candidate(0, 142)], analysis_version="v"
    )
    # Returns 70s segment
    service.compress_candidate.return_value = [EDLSegment(start_seconds=10, end_seconds=80)]
    result = AutoEditDirector.understand(service, "file", {"max_clip_seconds": 60, "min_clip_seconds": 5})
    assert result.clips[0].compression_attempted is True
    assert result.clips[0].compression_status == "FAILED"
    # Source segments remain original so prepare_candidates will drop it
    assert sum(s.end_seconds - s.start_seconds for s in result.clips[0].source_segments) == 142

def test_repair_produces_invalid_chronology():
    # 5. repair produces invalid chronological segments -> hard rejected
    service = MagicMock()
    service.analyze_video.return_value = GeminiAnalysisResponse(
        source_summary="Test", clips=[make_candidate(0, 142)], analysis_version="v"
    )
    # Overlapping segments: 10-30, 20-40
    service.compress_candidate.return_value = [
        EDLSegment(start_seconds=10, end_seconds=30),
        EDLSegment(start_seconds=20, end_seconds=40)
    ]
    result = AutoEditDirector.understand(service, "file", {"max_clip_seconds": 60, "min_clip_seconds": 5})
    assert result.clips[0].compression_status == "FAILED"

def test_repair_produces_timestamp_outside_source():
    # 4. repair produces timestamp outside source -> hard rejected
    service = MagicMock()
    service.analyze_video.return_value = GeminiAnalysisResponse(
        source_summary="Test", clips=[make_candidate(100, 242)], analysis_version="v"
    )
    # Starts at 50, but original was 100-242
    service.compress_candidate.return_value = [
        EDLSegment(start_seconds=50, end_seconds=70)
    ]
    result = AutoEditDirector.understand(service, "file", {"max_clip_seconds": 60, "min_clip_seconds": 5})
    assert result.clips[0].compression_status == "FAILED"
