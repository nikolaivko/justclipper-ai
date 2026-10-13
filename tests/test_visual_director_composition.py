import pytest
from app.services.visual_director import VisualDirector
from app.models.schemas import ClipCandidate, EDLSegment, SemanticVisualTarget

def test_single_person_talking_head():
    # If there's 1 person detected and no special events, it should use 'medium' (center_crop pan)
    clip = ClipCandidate.model_construct(
        title="Test",
        content_type="INTERVIEW",
        pacing_mode="STORY",
        source_segments=[EDLSegment(start_seconds=0, end_seconds=10)]
    )
    # mock a face track covering the segment
    face_tracks = [{"track_id": 1, "start_time": 0, "end_time": 10}]
    director = VisualDirector.for_pacing_mode(clip.pacing_mode)
    shots = director.build_shot_plan(clip, face_tracks, [], [])
    assert shots[0].composition == "medium"

def test_two_person_podcast():
    # If 2 people are detected, it should STILL use 'medium' (not fit_with_background fallback)
    clip = ClipCandidate.model_construct(
        title="Test",
        content_type="PODCAST",
        pacing_mode="STORY",
        source_segments=[EDLSegment(start_seconds=0, end_seconds=10)]
    )
    face_tracks = [
        {"track_id": 1, "start_time": 0, "end_time": 10},
        {"track_id": 2, "start_time": 0, "end_time": 10}
    ]
    director = VisualDirector.for_pacing_mode(clip.pacing_mode)
    shots = director.build_shot_plan(clip, face_tracks, [], [])
    assert shots[0].composition == "medium"

def test_wide_visual_document():
    # If the semantic event indicates a SCREEN or DOCUMENT, it should use fit_with_background
    clip = ClipCandidate.model_construct(
        title="Test",
        content_type="VLOG",
        pacing_mode="STORY",
        source_segments=[EDLSegment(start_seconds=0, end_seconds=10)],
        visual_events=[SemanticVisualTarget(
            start_seconds=0, end_seconds=10, visual_target_type="DOCUMENT",
            visual_target_description="A comic panel", importance=0.9, preferred_composition="screen_focus"
        )]
    )
    director = VisualDirector.for_pacing_mode(clip.pacing_mode)
    shots = director.build_shot_plan(clip, [], [], [])
    assert any(shot.composition == "fit_with_background" for shot in shots)

def test_mixed_short():
    # 0-5s talking head, 5-10s document, 10-15s talking head
    clip = ClipCandidate.model_construct(
        title="Test",
        content_type="VLOG",
        pacing_mode="STORY",
        source_segments=[EDLSegment(start_seconds=0, end_seconds=15)],
        visual_events=[SemanticVisualTarget(
            start_seconds=5, end_seconds=10, visual_target_type="DOCUMENT",
            visual_target_description="A comic panel", importance=0.9, preferred_composition="screen_focus"
        )]
    )
    face_tracks = [{"track_id": 1, "start_time": 0, "end_time": 15}]
    director = VisualDirector.for_pacing_mode(clip.pacing_mode)
    shots = director.build_shot_plan(clip, face_tracks, [], [])
    
    assert len(shots) >= 3
    # First part is talking head
    assert shots[0].composition == "medium"
    # Middle part is document (fit_with_background)
    assert any(shot.composition == "fit_with_background" for shot in shots)
    # End part is talking head
    assert shots[-1].composition == "medium"
