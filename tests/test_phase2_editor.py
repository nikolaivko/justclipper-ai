"""Editorial decisions and one real FFmpeg effect smoke render."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest

from app.models.schemas import (ClipCandidate, CutContinuity, EDLSegment,
                                EditorialScores, EffectEvent, HookVariant,
                                ImportancePoint,
                                MicroBeat, SemanticVisualTarget)
from app.services.editorial_effects import plan_effects, style_for, validate_edit_plan
from app.services.editorial_service import prepare_candidates
from app.services.ffmpeg_service import FFmpegService
from app.services.render_qc import check_render
from app.services.transcription_service import TranscriptionService
from app.services.visual_director import VisualDirector
from app.services.gemini_service import GeminiService
from app.models.schemas import GeminiAnalysisResponse


def candidate(**updates):
    data = dict(rank=1, title="real moment", short_description="result",
                topic="result", content_type="PODCAST", reason="observable reaction",
                categories=["REACTION"], context_dependency="LOW",
                source_segments=[EDLSegment(start_seconds=0, end_seconds=10),
                                 EDLSegment(start_seconds=12, end_seconds=24)],
                payoff_timestamp=18, payoff_present=True,
                scores=EditorialScores(first_1s_strength=8, first_3s_strength=8,
                                       standalone_clarity=8, payoff_strength=9,
                                       ending_strength=8))
    data.update(updates)
    return ClipCandidate(**data)


def effect(start, end, confidence=.95, kind="REACTION_PUSH"):
    return EffectEvent(effect_type=kind, start_seconds=start, end_seconds=end,
                       reason_code="REACTION_EMPHASIS", confidence=confidence,
                       intensity=.08)


@pytest.mark.parametrize("content,mode,style", [
    ("PODCAST", "CONVERSATIONAL", "PODCAST_DYNAMIC"),
    ("LATE_NIGHT", "CONVERSATIONAL", "PODCAST_DYNAMIC"),
    ("COMEDY", "COMEDIC", "COMEDY_TIMING"),
    ("STORY", "STORY", "STORY_DRIVEN"),
    ("TUTORIAL", "EDUCATIONAL", "EDUCATIONAL_FAST"),
    ("PRODUCT_DEMO", "VISUAL", "PRODUCT_DEMO"),
    ("GAMEPLAY", "ENERGETIC", "GAMEPLAY_DYNAMIC"),
    ("SPORT", "FAST", "ACTION_ENERGY"),
    ("EMOTIONAL_STORY", "EMOTIONAL", "EMOTIONAL_MINIMAL"),
])
def test_content_changes_editorial_style(content, mode, style):
    assert style_for(candidate(content_type=content, pacing_mode=mode)) == style


def test_effect_mapping_rejects_removed_section_and_low_confidence():
    clip = candidate(effect_events=[effect(13, 14), effect(10.5, 11.5),
                                    effect(20, 21, .4)])
    shots, plan = plan_effects(clip, [
        {"start": 0, "end": 10, "composition": "medium"},
        {"start": 12, "end": 24, "composition": "medium"}], [])
    assert len(plan["effect_events"]) == 1
    assert plan["effect_events"][0]["final_start"] == 11
    assert validate_edit_plan(clip, shots, plan["effect_events"], 24) is None
    wrong = [dict(shots[0]), dict(shots[1], start=11, end=23)]
    assert validate_edit_plan(clip, wrong, [], 24) == "SHOT_OUTSIDE_EDL"


def test_effect_budget_and_scene_cut_suppress_spam():
    clip = candidate(effect_events=[effect(13, 14), effect(15, 16),
                                    effect(17, 18), effect(20, 21)])
    shots = [{"start": 0, "end": 10, "composition": "medium"},
             {"start": 12, "end": 24, "composition": "medium"}]
    _, plan = plan_effects(clip, shots, [13])
    assert len(plan["effect_events"]) <= plan["effect_budget"]
    assert all(event["source_start"] != 13 for event in plan["effect_events"])
    assert plan["overediting_score"] > 0
    emotional = candidate(content_type="EMOTIONAL_STORY",
                          effect_events=[effect(13, 14)])
    _, quiet = plan_effects(emotional, shots, [])
    assert quiet["style_profile"] == "EMOTIONAL_MINIMAL"
    assert quiet["effect_events"] == []
    low_interest = candidate(effect_events=[effect(13, 14)],
                             importance_curve=[ImportancePoint(
                                 source_time=13.5, semantic_importance=2,
                                 emotional_intensity=1, reaction_strength=1)])
    assert plan_effects(low_interest, shots, [])[1]["effect_events"] == []


def test_reaction_shot_can_choose_listener_and_object_wins_when_requested():
    tracks = [{"time": t, "all_faces": [
        {"track_id": 1, "class_name": "person", "x": .1, "w": .1},
        {"track_id": 2, "class_name": "person", "x": .7, "w": .1},
        {"track_id": 3, "class_name": "cup", "x": .45, "w": .1}]}
        for t in (0, 2, 4, 6, 8)]
    clip = candidate(source_segments=[EDLSegment(start_seconds=0, end_seconds=10)],
                     payoff_timestamp=5,
                     visual_events=[
                         SemanticVisualTarget(start_seconds=0, end_seconds=5,
                             visual_target_type="LISTENER_REACTION",
                             visual_target_description="SPEAKER_B listens",
                             importance=.9, preferred_composition="medium"),
                         SemanticVisualTarget(start_seconds=5, end_seconds=10,
                             visual_target_type="OBJECT",
                             visual_target_description="cup",
                             importance=.9, preferred_composition="close")])
    shots = VisualDirector().build_shot_plan(clip, tracks, [])
    assert shots[0].target_track_ids == [2]
    assert shots[1].target_track_ids == [3]


def test_hook_variant_and_continuity_are_locally_gated():
    base = [EDLSegment(start_seconds=0, end_seconds=6),
            EDLSegment(start_seconds=8, end_seconds=10),
            EDLSegment(start_seconds=12, end_seconds=24)]
    good = HookVariant(strategy="REACTION_FIRST",
                       source_segments=[base[1], base[0], base[2]],
                       first_1s=10, first_3s=10, clarity=9, curiosity=9,
                       misleading_risk=1, payoff_preservation=9, continuity=9)
    selected = prepare_candidates([candidate(source_segments=base,
                                             hook_variants=[good])], 24)[0]
    assert selected.hook_strategy == "REACTION_FIRST"
    unsafe = HookVariant(strategy="PAYOFF_FIRST",
                         source_segments=list(reversed(base)),
                         first_1s=10, first_3s=10, clarity=9, curiosity=9,
                         misleading_risk=1, payoff_preservation=9, continuity=9)
    assert prepare_candidates([candidate(source_segments=base,
                                         hook_variants=[unsafe])], 24)[0].hook_strategy \
        == "NORMAL_CHRONOLOGICAL"
    bad_cut = CutContinuity(from_source_end=10, to_source_start=12,
                            semantic_continuity=2, visual_continuity=8,
                            audio_continuity=6, misleading_risk=8)
    assert "CUT_CONTINUITY_RISK" in prepare_candidates([candidate(cut_continuity=[bad_cut])], 24)[0].quality_flags


def test_caption_rhythm_changes_with_pacing_without_word_by_word_default():
    words = [SimpleNamespace(word=f"word{i}", start=i*.3, end=i*.3+.2)
             for i in range(8)]
    segment = SimpleNamespace(words=words)
    fast = TranscriptionService.group_words([segment], 0, "FAST")
    story = TranscriptionService.group_words([segment], 0, "STORY")
    assert [len(item["text"].split()) for item in fast] == [4, 4]
    assert [len(item["text"].split()) for item in story] == [6, 2]


def test_real_effect_render_keeps_audio_and_output_duration(tmp_path: Path):
    source = tmp_path / "source.mp4"
    dest = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=128x72:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "1.2", "-c:v", "libx264", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    shot = {"start": 0, "end": 1.0, "composition": "medium_close",
            "target_track_ids": [], "effect": {
                "effect_type": "REACTION_PUSH", "source_start": .2,
                "source_end": .8, "intensity": .06}}
    assert FFmpegService.render_clip(source, dest, {
        "source_segments": [{"start_seconds": 0, "end_seconds": 1}],
        "shot_plan": [shot]}, "center_crop", "720x1280")
    meta = FFmpegService.get_metadata(dest)
    assert meta.audio_codec == "aac"
    assert (meta.width, meta.height) == (720, 1280)
    assert check_render(dest, meta, [{**shot, "output_start": 0,
                                      "output_end": 1}], False) is None


def test_broad_deep_critic_passes_are_bounded_and_critic_is_text_only():
    service = GeminiService()
    calls = []
    original = candidate(source_segments=[EDLSegment(start_seconds=0, end_seconds=24)])
    detailed = candidate(source_segments=[EDLSegment(start_seconds=0, end_seconds=24)],
                         micro_beats=[MicroBeat(start_seconds=17,
                                               end_seconds=19,
                                               kind="REACTION",
                                               evidence="listener laughs")])

    def generate_content(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(text=GeminiAnalysisResponse(
                source_summary="real", clips=[original]).model_dump_json())
        if len(calls) == 2:
            return SimpleNamespace(text=GeminiAnalysisResponse(
                source_summary="focused", clips=[detailed]).model_dump_json())
        return SimpleNamespace(text='{"reviews":[{"rank":1,"issue_code":"NONE",'
                                    '"severity":0}]}')

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content))
    video = object()
    result = service.analyze_video(video, {"clip_count": 1, "source_duration": 24})
    assert len(calls) == 3
    assert calls[0]["contents"][0] is video
    assert calls[1]["contents"][0] is video
    assert isinstance(calls[2]["contents"], str)
    assert result.clips[0].micro_beats[0].kind == "REACTION"


def test_focused_pass_uses_uploaded_file_window():
    service = GeminiService()
    calls = []
    clip = candidate(source_segments=[EDLSegment(start_seconds=12,
                                                 end_seconds=24)])

    def generate_content(**kwargs):
        calls.append(kwargs)
        if len(calls) == 3:
            return SimpleNamespace(text='{"reviews":[]}')
        return SimpleNamespace(text=GeminiAnalysisResponse(
            source_summary="real", clips=[clip]).model_dump_json())

    def request(method, path, body, options):
        calls.append({"wire": body})
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": GeminiAnalysisResponse(
            source_summary="real", clips=[clip]).model_dump_json()}]}}]}

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content, api_client=SimpleNamespace(request=request)))
    video = SimpleNamespace(uri="https://files.example/video", mime_type="video/mp4")
    service.analyze_video(video, {"clip_count": 1, "source_duration": 30})
    part = calls[1]["wire"]["contents"][0]["parts"][0]
    assert part["fileData"]["fileUri"] == video.uri
    assert part["videoMetadata"]["startOffset"] == "10.000s"
    assert part["videoMetadata"]["endOffset"] == "26.000s"
