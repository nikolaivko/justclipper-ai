import pytest
from app.services.semantic_validation import timeline_audit, InvalidSemanticTimeline
from app.models.schemas import GeminiAnalysisResponse, ClipCandidate, EDLSegment

def test_timeline_validation_absolute_seconds():
    duration = 1870.65
    
    valid_near_end = ClipCandidate(
        title="Valid Near End", short_description="", content_type="", topic="", rank=1, reason="", categories=[], context_dependency="NONE",
        source_segments=[EDLSegment(start_seconds=1840, end_seconds=1868, reason="test")],
        hook_present=True, payoff_present=True, standalone_context=True,
    )
    
    invalid_out_of_bounds = ClipCandidate(
        title="Invalid Out of Bounds", short_description="", content_type="", topic="", rank=1, reason="", categories=[], context_dependency="NONE",
        source_segments=[EDLSegment(start_seconds=1840, end_seconds=1885, reason="test")],
        hook_present=True, payoff_present=True, standalone_context=True,
    )
    
    # 3110 implies MM:SS (31:10) which must NOT be automatically converted by production.
    invalid_mmss = ClipCandidate(
        title="Invalid MMSS", short_description="", content_type="", topic="", rank=1, reason="", categories=[], context_dependency="NONE",
        source_segments=[EDLSegment(start_seconds=3110, end_seconds=3140, reason="test")],
        hook_present=True, payoff_present=True, standalone_context=True,
    )
    
    res = GeminiAnalysisResponse(source_summary="test", clips=[valid_near_end, invalid_out_of_bounds, invalid_mmss])
    audit = timeline_audit(res, duration)
    
    assert audit[0]['valid'] == True
    assert audit[1]['valid'] == False
    assert "SOURCE_SEGMENT_OUT_OF_BOUNDS" in audit[1]['reasons']
    assert audit[2]['valid'] == False
    assert "SOURCE_SEGMENT_OUT_OF_BOUNDS" in audit[2]['reasons']
