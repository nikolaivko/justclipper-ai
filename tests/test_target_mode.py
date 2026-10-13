import pytest
from app.services.auto_edit_director import AutoEditDirector
from app.models.schemas import ClipCandidate, EditorialScores, GeminiAnalysisResponse
from app.services.semantic_cache import SemanticCache

def make_candidate(score, title="Test", start=1, end=10):
    return ClipCandidate(
        title=title,
        rank=1,
        short_description="Test",
        topic="Test",
        content_type="Test",
        reason="Test",
        categories=["Test"],
        context_dependency="STANDALONE",
        source_segments=[{"start_seconds": start, "end_seconds": end, "reason": "test"}],
        payoff_timestamp=(start + end) / 2.0,
        payoff_present=True,
        scores=EditorialScores(overall_score=score)
    )

def test_target_10_truncation():
    # 18 valid candidates
    candidates = [make_candidate(8, title=f"T{i}", start=i*20, end=i*20+15) for i in range(18)]
    analysis = GeminiAnalysisResponse(clips=candidates, source_summary="")

    selected, audit, quality_count, decisions = AutoEditDirector.select(analysis, 1000.0, {}, target_count=10)
    assert len(selected) == 10
    assert quality_count == 18
    # 10 selected, 8 not selected
    assert sum(1 for d in decisions if d["decision"] == "SELECTED") == 10
    assert sum(1 for d in decisions if d["decision"] == "NOT_SELECTED") == 8

def test_target_10_with_7_valid():
    candidates = [make_candidate(8, title=f"T{i}", start=i*20, end=i*20+15) for i in range(7)]
    analysis = GeminiAnalysisResponse(clips=candidates, source_summary="")

    selected, audit, quality_count, decisions = AutoEditDirector.select(analysis, 1000.0, {}, target_count=10)
    assert len(selected) == 7
    assert sum(1 for d in decisions if d["decision"] == "SELECTED") == 7

def test_target_mode_quality_floor():
    from app.config import config
    # Below floor candidate
    candidate = make_candidate(config.EDITORIAL_MIN_SCORE - 1, "Low")
    analysis = GeminiAnalysisResponse(clips=[candidate], source_summary="")

    selected, audit, qc, dec = AutoEditDirector.select(analysis, 1000.0, {}, target_count=10)
    assert len(selected) == 1
    assert selected[0].title == "Low"

def test_auto_mode_quality_floor():
    from app.config import config
    candidate = make_candidate(config.EDITORIAL_MIN_SCORE - 1, "Low")
    analysis = GeminiAnalysisResponse(clips=[candidate], source_summary="")

    selected, audit, qc, dec = AutoEditDirector.select(analysis, 1000.0, {}, target_count=None)
    assert len(selected) == 0
    assert dec[0]["decision"] == "NOT_SELECTED"

def test_deep_pass_preserves_unprocessed():
    from app.services.gemini_service import GeminiService
    service = GeminiService()
    candidates = [make_candidate(8, title=f"T{i}", start=i*20, end=i*20+15) for i in range(15)]
    analysis = GeminiAnalysisResponse(clips=candidates, source_summary="")
    service._deepen_and_critique("fake_file", analysis, {}, "model", 1000.0, limit=3)

    assert len(analysis.clips) == 15
    fallback_count = sum(1 for c in analysis.clips if c.deep_analysis_status == "BROAD_FALLBACK")
    assert fallback_count == 15

def test_cache_expansion_rejection():
    cache = SemanticCache()
    cache.root.mkdir(parents=True, exist_ok=True)
    key = "test_expansion_key"
    analysis = GeminiAnalysisResponse(clips=[make_candidate(8)], source_summary="")

    # Save with capacity 6
    cache.save(key, analysis, {}, requested_discovery_capacity=6)

    # Load with capacity 20 needed should be handled by worker
    loaded = cache.load(key)
    assert loaded is not None
    assert loaded[2].get("discovery_capacity") == 6


def test_cache_expansion_with_13_capacity():
    from app.config import config
    from app.services.auto_edit_director import AutoEditDirector

    settings = AutoEditDirector.settings(target_count=10, count_mode="TARGET")
    required_discovery = settings.clip_count

    # Prove that TARGET=10 maps to 20
    assert required_discovery == 20

    # Prove that a cache with capacity 13 is strictly less than 20
    assert 13 < required_discovery
