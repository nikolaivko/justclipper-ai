import pytest
from unittest.mock import patch, MagicMock

from app.services.auto_edit_director import AutoEditDirector
from app.models.schemas import ClipCandidate, RenderSettings
from app.services.clip_service import ClipService

def fake_clip(rank, start, end, scores_override=None, origin="GEMINI", payoff=True):
    from app.models.schemas import EDLSegment, EditorialScores
    from types import SimpleNamespace
    scores = EditorialScores()
    if scores_override:
        for k, v in scores_override.items():
            setattr(scores, k, v)
    # Ensure score is high enough
    for k in scores.model_dump().keys(): setattr(scores, k, 10)
    c = ClipCandidate(
        rank=rank,
        title=f"Title {rank}",
        short_description="Desc",
        topic="Topic",
        content_type="funny",
        reason="reason",
        categories=[],
        context_dependency="low",
        source_segments=[EDLSegment(start_seconds=start, end_seconds=end)],
        scores=scores,
        selection_origin=origin,
        payoff_present=payoff,
        payoff_timestamp=end - 1 if payoff else None
    )
    c.editorial_score = sum(getattr(scores, k) for k in scores.model_dump().keys()) / 35.0  # just a fake computation
    return c

class FakeAnalysis:
    def __init__(self, clips):
        self.clips = clips
        self.events = []
        self.narrative_relationships = []

def test_auto_edit_director_auto_mode():
    settings = AutoEditDirector.settings(None, None, "AUTO")
    assert settings.count_mode == "AUTO"
    assert settings.target_clip_count is None
    assert settings.clip_count >= 8

def test_auto_edit_director_target_mode():
    for target in [1, 2, 3, 5, 10]:
        settings = AutoEditDirector.settings(None, target, "TARGET")
        assert settings.count_mode == "TARGET"
        assert settings.target_clip_count == target
        assert settings.clip_count >= max(8, target + 3)

def test_select_respects_target_count():
    clips = [fake_clip(i, i*10, i*10+10, {"hook_strength": 9, "standalone_clarity": 9}) for i in range(1, 15)]
    analysis = FakeAnalysis(clips)
    
    # Auto mode should cap at config.AUTO_EDIT_MAX_CANDIDATES (default 8)
    from app.config import config
    selected, audit, count, _ = AutoEditDirector.select(analysis, 1000.0, {}, target_count=None)
    assert len(selected) == config.AUTO_EDIT_MAX_CANDIDATES
    assert count == len(clips) # all 14 passed quality
    
    # Target mode should cap at target_count
    for target in [1, 2, 3, 5, 10]:
        clips = [fake_clip(i, i*10, i*10+10, {"hook_strength": 9, "standalone_clarity": 9}) for i in range(1, 15)]
        analysis = FakeAnalysis(clips)
        selected, audit, count, _ = AutoEditDirector.select(analysis, 1000.0, {}, target_count=target)
        assert len(selected) == target
        assert count == len(clips)

def test_requested_5_but_only_3_valid():
    # 3 valid, 2 invalid (too short)
    clips = [
        fake_clip(1, 10, 20),
        fake_clip(2, 30, 40),
        fake_clip(3, 50, 60),
        fake_clip(4, 70, 72), # 2 seconds, < 5 sec min
        fake_clip(5, 80, 81), # 1 second, < 5 sec min
    ]
    analysis = FakeAnalysis(clips)
    selected, audit, count, _ = AutoEditDirector.select(analysis, 100.0, {}, target_count=5)
    assert len(selected) == 3
    assert count == 3
    assert "UNUSABLE_DURATION" in audit

def test_deduplication_preserved():
    # 3 valid but overlapping
    clips = [
        fake_clip(1, 10, 30),
        fake_clip(2, 15, 35), # overlaps with 1
        fake_clip(3, 50, 70),
    ]
    analysis = FakeAnalysis(clips)
    selected, audit, count, _ = AutoEditDirector.select(analysis, 100.0, {}, target_count=5)
    assert len(selected) == 2 # one overlap removed

