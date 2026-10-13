import pytest
import pydantic
from app.models.schemas import GeminiAnalysisResponse

def test_effect_events_missing_confidence():
    """Reproduces the exact ValidationError seen in production where Gemini omitted the confidence field."""
    # This was failing before the fix: confidence was required in Pydantic but not in Gemini schema.
    # Now it should parse successfully because confidence is Optional[float] = None.
    mock_json = {
        "source_summary": "Test",
        "clips": [
            {
                "rank": 2,
                "title": "Buying a Full Middle Aisle Outfit",
                "short_description": "Desc",
                "topic": "Topic",
                "content_type": "Type",
                "reason": "Reason",
                "categories": ["STORY"],
                "context_dependency": "standalone",
                "source_segments": [{"start_seconds": 141.0, "end_seconds": 218.0, "reason": "Presents"}],
                "payoff_present": True,
                "payoff_timestamp": 218.0,
                "resolution_present": True,
                "intentional_unresolved_ending": False,
                "ending_quality": 5,
                "required_context": [],
                "preserve_pauses": [],
                "reaction_moments": [],
                "visual_events": [],
                "micro_beats": [],
                "hook_variants": [{"strategy": "S", "source_segments": [{"start_seconds": 1, "end_seconds": 2, "reason": "Test"}]}],
                "protected_regions": [],
                "effect_events": [{"effect_type": "zoom", "start_seconds": 141.0, "end_seconds": 145.0, "reason_code": "emphasis"}],
                "first_frame_reason": "Test",
                "ending_reason": "Test"
            }
        ]
    }
    
    # Should not raise exception
    response = GeminiAnalysisResponse.model_validate(mock_json)
    assert response.clips[0].effect_events[0].confidence is None

