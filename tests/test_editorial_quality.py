from pathlib import Path
from types import SimpleNamespace
import subprocess
import json

import pytest

from app.models.schemas import (ClipCandidate, EDLSegment, EditorialScores,
                                NarrativeEvent, NarrativeRelationship,
                                SemanticVisualTarget, ContextOverlay)
from app.services.editorial_service import (prepare_candidates, score_candidate,
                                            validate_final_edl)
from app.services.timeline import remap_intervals, with_output_times
from app.services.transcription_service import TranscriptionService
from app.services.visual_director import VisualDirector
from app.services.crop_service import SmoothedCameraman, CropSmoothingService
from app.services.semantic_cache import SemanticCache
from app.services.visual_analysis_cache import VisualAnalysisCache
from app.services.analysis_cache_pruning import prune_analysis_cache
from app.models.schemas import GeminiAnalysisResponse
from app.config import config
from app.services.gemini_service import GeminiService, PROMPT_VERSION
from app.services.job_store import job_store
from app.services.worker import PipelineWorker
from app.services.clip_service import ClipService
from app.services.ffmpeg_service import FFmpegService


def moment(**kwargs):
    data = dict(rank=1, title="A complete moment", short_description="A result is shown.",
                topic="result", content_type="PODCAST", reason="complete beat",
                categories=["REACTION"], context_dependency="LOW",
                source_segments=[EDLSegment(start_seconds=10, end_seconds=15)],
                payoff_present=True, payoff_timestamp=14.0,
                scores=EditorialScores(first_1s_strength=8, first_3s_strength=8,
                                       standalone_clarity=8, payoff_strength=9,
                                       ending_strength=8))
    data.update(kwargs)
    return ClipCandidate(**data)


def test_graph_setup_is_inserted_and_boring_gap_is_removed():
    candidate = moment(source_segments=[EDLSegment(start_seconds=42, end_seconds=55)],
                       payoff_timestamp=50)
    events = [NarrativeEvent(event_id="setup", start_seconds=35, end_seconds=39,
                             kind="SETUP", description="Explains the challenge"),
              NarrativeEvent(event_id="payoff", start_seconds=48, end_seconds=52,
                             kind="PAYOFF", description="Unexpected result")]
    graph = [NarrativeRelationship(from_event_id="setup", to_event_id="payoff",
                                   kind="SETUP_PAYOFF")]
    selected = prepare_candidates([candidate], 60, events=events, relationships=graph)
    assert [(s.start_seconds, s.end_seconds) for s in selected[0].source_segments] == [
        (35, 39), (42, 55)]
    assert not any(s.start_seconds <= 40 < s.end_seconds for s in selected[0].source_segments)
    assert selected[0].required_context
    assert validate_final_edl(selected[0], 60) is None


def test_context_does_not_become_the_ending_after_payoff_cold_open():
    candidate = moment(hook_strategy="REACTION_FIRST",
                       required_context=[EDLSegment(start_seconds=3, end_seconds=5)])
    result = prepare_candidates([candidate], 30)[0]
    assert [(seg.start_seconds, seg.end_seconds) for seg in result.source_segments] == [
        (3, 5), (10, 15)]


def test_payoff_cannot_be_excluded_even_with_good_hook():
    candidate = moment(payoff_timestamp=20)
    assert prepare_candidates([candidate], 30) == []
    assert validate_final_edl(candidate, 30) == "PAYOFF_MISSING"


def test_payoff_at_final_cut_boundary_is_incomplete():
    candidate = moment(payoff_timestamp=15)
    assert prepare_candidates([candidate], 30) == []
    assert validate_final_edl(candidate, 30) == "PAYOFF_MISSING"


def test_explicit_missing_payoff_is_rejected():
    assert prepare_candidates([moment(payoff_present=False)], 30) == []


def test_reaction_after_punchline_is_preserved():
    candidate = moment(reaction_moments=[EDLSegment(start_seconds=15.3,
                                                   end_seconds=17.2)])
    result = prepare_candidates([candidate], 30)[0]
    assert result.source_segments[-1].end_seconds == 17.2
    assert "reaction" in result.source_segments[-1].reason.lower()


@pytest.mark.parametrize("mode,protected,expected_split", [
    ("EDUCATIONAL", [], True),
    ("COMEDIC", [EDLSegment(start_seconds=11, end_seconds=13,
                            reason="PRESERVE_PAUSE_FOR_COMEDY")], False),
    ("TENSION", [], False),
    ("EMOTIONAL", [EDLSegment(start_seconds=11, end_seconds=13,
                             reason="PRESERVE_PAUSE_FOR_EMOTION")], False),
])
def test_dead_air_respects_pacing_and_meaning(mode, protected, expected_split):
    candidate = moment(pacing_mode=mode, preserve_pauses=protected, source_segments=[EDLSegment(start_seconds=10, end_seconds=30)], payoff_timestamp=25.0)
    result = prepare_candidates([candidate], 30, {"silences": [{"start": 11, "end": 13}]})[0]
    assert (len(result.source_segments) == 2) is expected_split


def test_expensive_context_and_weak_ending_lower_retention_score():
    strong = moment()
    costly = moment(scores=strong.scores.model_copy(update={
        "context_cost": 9, "early_swipe_risk": 8, "weak_ending_penalty": 9}))
    assert score_candidate(costly) < score_candidate(strong)


def test_good_hook_without_payoff_is_penalized():
    strong = moment()
    weak = moment(payoff_present=False, payoff_timestamp=None)
    assert score_candidate(weak) <= score_candidate(strong) - 40


def test_weak_candidates_do_not_fill_requested_clip_count():
    weak = moment(scores=EditorialScores(first_1s_strength=2,
                                         standalone_clarity=3,
                                         payoff_strength=3))
    result = prepare_candidates([weak], 30)
    assert len(result) == 1
    assert "BELOW_MIN_SCORE" in result[0].quality_flags


def test_failed_analysis_diagnostics_counts_rejections_without_content(
        tmp_path, monkeypatch):
    from app.api.routes import get_analysis_diagnostics

    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_CACHE", tmp_path / "cache")
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not a real video")
    job = ClipService.create_job("auditjob", "source.mp4", source)
    job["status"] = "FAILED"
    job["metadata"] = {"duration": 30.0}
    cache = SemanticCache()
    job["semantic_cache_key"] = cache.key(source, {"clip_count": 2})
    job_store.set(job["id"], job)
    cache.save(job["semantic_cache_key"], GeminiAnalysisResponse(
        source_summary="Private source text",
        clips=[moment(), moment(rank=2, title="Private clip text", payoff_present=False)]),
        {"silences": [], "scenes": [], "speech": []})

    result = get_analysis_diagnostics(job["id"])
    assert result == {"cache_available": True, "raw_candidates": 2,
                      "selected_candidates": 1,
                      "outcomes": {"PAYOFF_MISSING": 1, "SELECTED": 1}}
    assert "Private" not in str(result)

    job["status"] = "READY_TO_RENDER"
    job_store.set(job["id"], job)
    assert get_analysis_diagnostics(job["id"]) == result


def test_near_duplicate_payoff_is_deduplicated():
    a = moment(title="first")
    b = moment(title="second", source_segments=[EDLSegment(start_seconds=11, end_seconds=16)],
               payoff_timestamp=14.5)
    assert len(prepare_candidates([a, b], 30)) == 1


def test_context_overlay_requires_named_source_evidence():
    overlay = ContextOverlay(text="First attempt", evidence_event_id="attempt",
                             start_seconds=10, end_seconds=12)
    unsupported = prepare_candidates([moment(context_overlay=overlay)], 30)[0]
    assert unsupported.context_overlay is None
    event = NarrativeEvent(event_id="attempt", start_seconds=3, end_seconds=5,
                           kind="SETUP", description="First attempt")
    supported = prepare_candidates([moment(context_overlay=overlay)], 30,
                                   events=[event])[0]
    assert supported.context_overlay.text == "First attempt"


def test_noncontiguous_edl_remaps_caption_crop_and_zoom_events():
    shots = with_output_times([
        {"start": 35, "end": 39, "zoom_intent": "NONE"},
        {"start": 42, "end": 55, "zoom_intent": "SUBTLE_IN"}])
    assert shots[1]["output_start"] == 4
    caption = remap_intervals(shots, [{"start_seconds": 43, "end_seconds": 45,
                                       "text": "the result"}])
    crop = remap_intervals(shots, [{"start_seconds": 48, "end_seconds": 50,
                                    "target": "object"}])
    zoom = remap_intervals(shots, [{"start_seconds": 42, "end_seconds": 44,
                                    "scale": 1.08}])
    assert (caption[0]["start_seconds"], caption[0]["end_seconds"]) == (5, 7)
    assert (crop[0]["start_seconds"], crop[0]["end_seconds"]) == (10, 12)
    assert (zoom[0]["start_seconds"], zoom[0]["end_seconds"]) == (4, 6)


def test_remap_splits_event_across_a_cut():
    shots = [{"start": 0, "end": 2}, {"start": 4, "end": 6}]
    assert [(e["start_seconds"], e["end_seconds"]) for e in remap_intervals(
        shots, [{"start_seconds": 1, "end_seconds": 5}])] == [(1, 2), (2, 3)]


def test_phrase_caption_segmentation_uses_punctuation_and_silence():
    words = [SimpleNamespace(word=t, start=s, end=e) for t, s, e in [
        ("Wait,", 0, .2), ("this", .22, .4), ("works!", .42, .7),
        ("Really", 1.4, 1.7), ("well.", 1.72, 2.0)]]
    captions = TranscriptionService.group_words([SimpleNamespace(words=words)], 10)
    assert [item["text"] for item in captions] == ["Wait, this works!", "Really well."]
    assert captions[-1]["end_seconds"] == 12


def test_object_can_be_primary_and_two_people_keep_context():
    tracks = [{"time": 10, "all_faces": [
        {"track_id": 1, "class_name": "person", "x": .1, "w": .15},
        {"track_id": 2, "class_name": "person", "x": .7, "w": .15},
        {"track_id": 3, "class_name": "cup", "x": .45, "w": .1}]}]
    visual_events = [SemanticVisualTarget(start_seconds=10, end_seconds=12,
        visual_target_type="OBJECT", visual_target_description="cup",
        importance=.9, preferred_composition="close"),
        SemanticVisualTarget(start_seconds=12, end_seconds=15,
        visual_target_type="MULTIPLE_PEOPLE", visual_target_description="two people",
        importance=.8, preferred_composition="medium")]
    shots = VisualDirector().build_shot_plan(moment(visual_events=visual_events), tracks, [])
    assert shots[0].target_track_ids == [3]
    assert shots[1].composition == "medium"


def test_scene_boundary_does_not_merge_shots():
    events = [SemanticVisualTarget(start_seconds=10, end_seconds=11,
        visual_target_type="ACTION", visual_target_description="ball",
        importance=.8, preferred_composition="wide"),
        SemanticVisualTarget(start_seconds=11, end_seconds=12,
        visual_target_type="REACTION", visual_target_description="player",
        importance=.8, preferred_composition="medium")]
    shots = VisualDirector(min_shot_duration=1.5).build_shot_plan(
        moment(visual_events=events), [], [], [11])
    assert len(shots) >= 2


def test_scene_cut_splits_fallback_tracking_even_without_semantic_events():
    shots = VisualDirector().build_shot_plan(moment(visual_events=[]), [], [], [12])
    assert [(shot.start, shot.end) for shot in shots] == [(10, 12), (12, 15)]


def test_short_shot_cannot_bridge_a_removed_source_gap():
    candidate = moment(source_segments=[EDLSegment(start_seconds=10, end_seconds=12),
                                        EDLSegment(start_seconds=20, end_seconds=21)],
                       payoff_timestamp=20.5,
                       visual_events=[SemanticVisualTarget(
                           start_seconds=20, end_seconds=21,
                           visual_target_type="OBJECT", visual_target_description="cup",
                           importance=.9, preferred_composition="close")])
    shots = VisualDirector().build_shot_plan(candidate, [], [])
    assert [(shot.start, shot.end) for shot in shots] == [(10, 12), (20, 21)]


def test_target_loss_holds_crop_and_scene_reset_restarts_camera():
    camera = SmoothedCameraman(600, 1920)
    camera.update_target(400)
    x = camera.get_crop_x()
    camera.update_target(None)
    assert camera.get_crop_x() == x
    camera.begin_scene()
    camera.update_target(1500)
    assert camera.get_crop_x() > x


def test_multi_subject_crop_targets_group_center(tmp_path):
    tracks = [{"time": 0.0, "all_faces": [
        {"track_id": 1, "x": .1, "w": .1},
        {"track_id": 2, "x": .7, "w": .1}]}]
    path = tmp_path / "crop.txt"
    CropSmoothingService.generate_shot_sendcmd(
        {"start": 0, "end": .2, "target_track_ids": [1, 2]},
        tracks, 1920, 600, str(path))
    assert "crop@c x" in path.read_text()


def test_single_frame_shot_has_a_valid_crop_command(tmp_path):
    path = tmp_path / "crop.txt"
    CropSmoothingService.generate_shot_sendcmd(
        {"start": 260.0, "end": 260.025, "target_track_ids": [1]},
        [{"time": 260.0, "all_faces": [
            {"track_id": 1, "x": .4, "w": .1}]}],
        1920, 608, str(path), fps=30)
    assert path.read_text().startswith("0.000 crop@c x ")


def test_motion_burst_does_not_become_one_frame_shots():
    candidate = moment(content_type="SOURCE_EXCERPT",
                       source_segments=[EDLSegment(start_seconds=380, end_seconds=400)],
                       payoff_timestamp=None, visual_events=[])
    scene_cuts = [383.65, 383.683333, 383.783333, 383.816667,
                  384.216667, 386.65, 386.683333, 388.883333,
                  388.916667, 396.683333, 396.85, 399.0]
    shots = VisualDirector.for_pacing_mode("CONVERSATIONAL").build_shot_plan(
        candidate, [], [], scene_cuts)
    assert [(shot.start, shot.end) for shot in shots] == [
        (380, 383.65), (383.65, 386.65), (386.65, 388.883333),
        (388.883333, 396.683333), (396.683333, 400)]


def test_semantic_cache_depends_on_source_hash_and_analysis_settings(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video A")
    cache = SemanticCache(tmp_path / "cache")
    key = cache.key(source, {"clip_count": 3})
    result = GeminiAnalysisResponse(source_summary="test", clips=[moment()])
    cache.save(key, result, {"scenes": [1.0]})
    assert cache.load(key)[1] == {"scenes": [1.0]}
    assert score_candidate(cache.load(key)[0].clips[0]) == score_candidate(result.clips[0])
    assert cache.key(source, {"min_clip_seconds": 20}) != key
    assert cache.key(source, {"clip_count": 3, "exact_clip_count": True}) != key
    source.write_bytes(b"video B")
    assert cache.key(source, {"clip_count": 3}) != key


def test_visual_cache_depends_on_selected_edl(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    cache = VisualAnalysisCache(tmp_path / "visual")
    key = cache.key(source, [(10, 15)], "source-hash")
    cache.save(key, {"tracks": [], "scenes": [12], "speech": []})
    assert cache.load(key)["scenes"] == [12]
    assert cache.key(source, [(10, 16)], "source-hash") != key


def test_analysis_cache_pruning_stays_inside_cache_and_protects_oauth(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    root = tmp_path / "cache" / "semantic"
    root.mkdir(parents=True)
    protected = tmp_path / "youtube" / "token.json"
    protected.parent.mkdir()
    protected.write_text("protected")
    (root / "a.json").write_text("a")
    (root / "b.json").write_text("b")
    (root / "linked.json").symlink_to(protected)
    prune_analysis_cache(root, max_files=1)
    assert len([path for path in root.glob("*.json") if not path.is_symlink()]) == 1
    assert protected.read_text() == "protected"


@pytest.mark.parametrize("content_type,mode,visual_type,expected", [
    ("PODCAST", "CONVERSATIONAL", "ACTIVE_SPEAKER", "medium"),
    ("VLOG", "COMEDIC", "REACTION", "medium"),
    ("TUTORIAL", "EDUCATIONAL", "OBJECT", "fit_with_background"),
    ("GAMEPLAY", "ENERGETIC", "GAMEPLAY", "fit_with_background"),
    ("PRODUCT_DEMO", "VISUAL", "OBJECT", "fit_with_background"),
    ("SPORT", "FAST", "ACTION", "fit_with_background"),
    ("STORY", "EMOTIONAL", "ACTIVE_SPEAKER", "medium"),
    ("SCREEN_RECORDING", "EDUCATIONAL", "SCREEN", "fit_with_background"),
])
def test_content_fixtures_follow_semantic_subject_and_pacing(
        content_type, mode, visual_type, expected):
    event = SemanticVisualTarget(start_seconds=10, end_seconds=15,
        visual_target_type=visual_type, visual_target_description="subject",
        importance=.8, preferred_composition="medium")
    candidate = moment(content_type=content_type, pacing_mode=mode,
                       visual_events=[event])
    chosen = prepare_candidates([candidate], 30)[0]
    tracks = [{"time": 12, "all_faces": [{"track_id": 1, "x": .4, "y": .2, "w": .2, "h": .3}]}]
    shots = VisualDirector().build_shot_plan(chosen, tracks, [])
    assert chosen.pacing_mode == mode
    assert shots[0].composition == expected


def test_pacing_changes_minimum_shot_length_without_forcing_cuts():
    assert (VisualDirector.for_pacing_mode("EMOTIONAL").min_shot_duration >
            VisualDirector.for_pacing_mode("FAST").min_shot_duration)
    candidate = moment(visual_events=[])
    assert len(VisualDirector.for_pacing_mode("FAST").build_shot_plan(
        candidate, [], [])) == 1


def test_zoomed_original_render_has_valid_audio_video_and_duration(tmp_path):
    source = tmp_path / "original.mp4"
    output = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=green:s=128x72:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "0.8", "-c:v", "libx264", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    assert FFmpegService.render_clip(source, output, {
        "source_segments": [{"start_seconds": 0, "end_seconds": .7}],
        "shot_plan": [{"start": 0, "end": .7, "target_track_ids": [],
                       "composition": "medium", "zoom_intent": "SUBTLE_IN"}],
        "subtitles": [{"start_seconds": .1, "end_seconds": .5,
                       "text": "A real line", "emphasis": ["real"]}],
        "context_overlay": {"start_seconds": 0, "end_seconds": .6,
                            "text": "First attempt"},
    }, "center_crop", "720x1280")
    meta = FFmpegService.get_metadata(output)
    assert (meta.width, meta.height) == (720, 1280)
    assert meta.video_codec == "h264"
    assert meta.audio_codec == "aac"
    assert .6 <= meta.duration <= .8
    pixel_format = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=pix_fmt", "-of", "default=noprint_wrappers=1",
        str(output)], check=True, capture_output=True, text=True).stdout
    assert "pix_fmt=yuv420p" in pixel_format
    atoms = output.read_bytes()
    assert atoms.find(b"moov") < atoms.find(b"mdat")


def test_short_tracked_shot_renders_and_keeps_60fps_source_motion(tmp_path):
    source = tmp_path / "60fps-source.mp4"
    output = tmp_path / "tracked.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=128x72:r=60",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "0.8", "-c:v", "libx264", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    assert FFmpegService.render_clip(source, output, {
        "source_segments": [{"start_seconds": 0, "end_seconds": .7}],
        "shot_plan": [{"start": 0, "end": .025, "target_track_ids": [1]},
                      {"start": .025, "end": .7, "target_track_ids": [1]}],
        "face_tracks": [{"time": 0, "all_faces": [
            {"track_id": 1, "x": .4, "y": .3, "w": .1, "h": .2}]}],
        "subtitles": [],
    }, "center_crop", "720x1280")
    meta = FFmpegService.get_metadata(output)
    assert meta.fps == 60
    assert .65 <= meta.duration <= .75


def test_caption_layout_is_two_lines_safe_and_emphasis_is_selective(tmp_path):
    from PIL import Image

    data = FFmpegService._create_subtitle_images([{
        "start_seconds": 0, "end_seconds": 1,
        "text": "I NEVER expected this surprising result",
        "emphasis": ["NEVER"]}], tmp_path, 1, 1080, "caption-test")
    overlay = Image.open(data["images"][1]["path"]).convert("RGBA")
    alpha = overlay.getchannel("A")
    bounds = alpha.getbbox()
    assert bounds is not None
    assert 0 <= bounds[0] < bounds[2] <= 1080
    assert bounds[3] - bounds[1] < 200
    # The overlay is placed at 75% of the frame, inside the lower safe zone.
    assert .75 * 1920 + bounds[3] < .9 * 1920
    pixels = list(overlay.getdata())
    assert any(r > g > b and r > 200 for r, g, b, a in pixels if a > 200)


def test_mixed_zoom_and_plain_cuts_keep_final_duration(tmp_path):
    source = tmp_path / "original25.mp4"
    output = tmp_path / "two-shots.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=128x72:r=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "1", "-c:v", "libx264", "-c:a", "aac", str(source)],
                   check=True, capture_output=True)
    assert FFmpegService.render_clip(source, output, {
        "source_segments": [{"start_seconds": 0, "end_seconds": .4},
                            {"start_seconds": .5, "end_seconds": .9}],
        "shot_plan": [{"start": 0, "end": .4, "composition": "medium",
                       "zoom_intent": "NONE"},
                      {"start": .5, "end": .9, "composition": "medium",
                       "zoom_intent": "SUBTLE_OUT"}],
    }, "center_crop", "720x1280")
    meta = FFmpegService.get_metadata(output)
    assert .72 <= meta.duration <= .88
    assert meta.audio_codec == "aac"


def test_gemini_request_uses_video_and_structured_editorial_schema():
    service = GeminiService()
    captured = {}

    def generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text=json.dumps(GeminiAnalysisResponse(
            source_summary="A real reaction", clips=[moment()]).model_dump()))

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content))
    video = object()
    result = service.analyze_video(video, {"clip_count": 3,
                                           "local_analysis": '{"scenes":[10]}'})
    assert result.analysis_version == PROMPT_VERSION
    assert captured["contents"][0] is video
    assert "scenes" in captured["contents"][1]
    props = captured["config"].response_schema["properties"]
    assert "narrative_relationships" in props
    clip_props = props["clips"]["items"]["properties"]
    assert {"scores", "required_context", "reaction_moments",
            "payoff_timestamp", "pacing_mode"} <= clip_props.keys()


def test_empty_gemini_result_gets_one_focused_pass():
    service = GeminiService()
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        clips = [] if len(calls) == 1 else [moment()]
        return SimpleNamespace(text=GeminiAnalysisResponse(
            source_summary="real summary", clips=clips).model_dump_json())

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content))
    result = service.analyze_video(object(), {"clip_count": 30})
    assert len(calls) == 2
    assert "previous pass returned no clip candidates" in calls[1]["contents"][1]
    assert len(result.clips) == 1


def test_second_empty_gemini_result_stops_after_one_focused_pass():
    service = GeminiService()
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=GeminiAnalysisResponse(
            source_summary="No complete moments", clips=[]).model_dump_json())

    service.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content))
    result = service.analyze_video(object(), {"clip_count": 30})
    assert len(calls) == 2
    assert result.clips == []


def test_analysis_cache_hit_skips_proxy_upload_and_local_reanalysis(
        tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"dummy source")
    monkeypatch.setattr(config, "DATA_CACHE", cache_dir)
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    job = ClipService.create_job("cachejob", "source.mp4", source)
    job["metadata"] = {"duration": 30.0, "file_size": len(source.read_bytes())}
    job["analysis_settings"] = {"clip_count": 1}
    job_store.set("cachejob", job)
    cache = SemanticCache()
    key = cache.key(source, job["analysis_settings"])
    cache.save(key, GeminiAnalysisResponse(source_summary="cached", clips=[moment()], analysis_version=PROMPT_VERSION), {"silences": [], "scenes": [], "speech": []}, 1)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("expensive analysis ran on cache hit")

    monkeypatch.setattr(GeminiService, "upload_video", forbidden)
    monkeypatch.setattr(ClipService, "run_local_analysis", forbidden)
    from app.services.ffmpeg_service import FFmpegService
    monkeypatch.setattr(FFmpegService, "create_proxy", forbidden)
    PipelineWorker()._run_analysis(job)
    saved = job_store.get("cachejob")
    assert saved["status"] == "READY_TO_RENDER"
    assert saved["candidates"][0]["editorial_score"] > 0
    assert saved["analysis_version"] == PROMPT_VERSION


def test_existing_empty_cache_gets_one_recovery_analysis(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"small source")
    monkeypatch.setattr(config, "DATA_CACHE", cache_dir)
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    job = ClipService.create_job("emptycachejob", "source.mp4", source)
    job["metadata"] = {"duration": 30.0, "file_size": source.stat().st_size}
    job["analysis_settings"] = {"clip_count": 30}
    job_store.set(job["id"], job)
    cache = SemanticCache()
    key = cache.key(source, job["analysis_settings"])
    cache.save(key, GeminiAnalysisResponse(source_summary="empty", clips=[],
                                           analysis_version=PROMPT_VERSION),
               {"silences": [], "scenes": [], "speech": []})

    calls = []

    def local_analysis(job_id, _path, _duration):
        saved = job_store.get(job_id)
        saved["analysis_data"] = {"silences": [], "scenes": [], "speech": []}
        job_store.set(job_id, saved)

    def analyze(*_args):
        calls.append("gemini")
        return GeminiAnalysisResponse(source_summary="complete", clips=[moment()],
                                      analysis_version=PROMPT_VERSION)

    monkeypatch.setattr(ClipService, "run_local_analysis", local_analysis)
    monkeypatch.setattr(GeminiService, "upload_video", lambda *a, **kw: SimpleNamespace(name="remote"))
    monkeypatch.setattr(GeminiService, "delete_video", lambda *a, **kw: None)
    monkeypatch.setattr(GeminiService, "analyze_video", analyze)
    PipelineWorker()._run_analysis(job)
    saved = job_store.get(job["id"])
    assert calls == ["gemini"]
    assert saved["empty_cache_recovery_attempted"] is True
    assert saved["status"] == "READY_TO_RENDER"
    assert len(saved["candidates"]) == 1


def test_empty_cache_after_recovery_reports_specific_reason(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"small source")
    monkeypatch.setattr(config, "DATA_CACHE", cache_dir)
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    job = ClipService.create_job("still_empty", "source.mp4", source)
    job["metadata"] = {"duration": 30.0, "file_size": source.stat().st_size}
    job["analysis_settings"] = {"clip_count": 30}
    job["empty_cache_recovery_attempted"] = True
    job["lifecycle_version"] = 0
    job_store.set(job["id"], job)
    cache = SemanticCache()
    cache.save(cache.key(source, job["analysis_settings"]), GeminiAnalysisResponse(source_summary="empty", clips=[], analysis_version=PROMPT_VERSION), {"silences": [], "scenes": [], "speech": []}, 30)
    monkeypatch.setattr(GeminiService, "upload_video", lambda *a, **kw: pytest.fail("unexpected upload"))
    PipelineWorker()._process_analysis(job)
    saved = job_store.get(job["id"])
    assert saved["status"] == "FAILED"
    assert saved["error"] == "NO_SUITABLE_CLIPS"


def test_successful_retry_clears_prior_analysis_error(tmp_path, monkeypatch):
    from app.api.routes import get_status

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    job = ClipService.create_job("retried", "source.mp4", tmp_path / "source.mp4")
    ClipService.update_job_status(job["id"], "FAILED", "ANALYSIS_FAILED")
    assert get_status(job["id"])["error"] == "ANALYSIS_FAILED"
    ClipService.update_job_status(job["id"], "QUEUED_ANALYSIS")
    ClipService.update_job_status(job["id"], "READY_TO_RENDER")
    assert "error" not in job_store.get(job["id"])
    assert "failed_at" not in job_store.get(job["id"])
    assert "error" not in get_status(job["id"])


def test_status_hides_stale_error_on_existing_successful_job(tmp_path, monkeypatch):
    from app.api.routes import get_status

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs_dir)
    job = ClipService.create_job("legacy_success", "source.mp4", tmp_path / "source.mp4")
    job.update(status="READY_TO_RENDER", error="ANALYSIS_FAILED", failed_at=1)
    job_store.set(job["id"], job)
    public = get_status(job["id"])
    assert public["status"] == "READY_TO_RENDER"
    assert "error" not in public
    assert "failed_at" not in public


@pytest.mark.parametrize('start,end', [(float('nan'), 5), (0, float('inf')), (float('-inf'), 5)])
def test_nonfinite_edl_cannot_reach_ffmpeg(start, end):
    assert validate_final_edl(moment(source_segments=[EDLSegment(start_seconds=start, end_seconds=end)]), 60) == 'INVALID_EDL_SEGMENT'


def test_tall_silent_portrait_renders_with_audio_and_without_caption_input(tmp_path):
    source = tmp_path / 'portrait.mp4'
    dest = tmp_path / 'short.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'testsrc2=size=72x160:rate=60', '-t', '0.5', '-c:v', 'libx264', str(source)],
                   check=True, capture_output=True)
    assert FFmpegService.render_clip(source, dest, {
        'source_segments': [{'start_seconds': 0, 'end_seconds': .5}],
        'shot_plan': [{'start': 0, 'end': .5, 'composition': 'medium'}],
        'subtitles': []}, 'center_crop', '720x1280')
    meta = FFmpegService.get_metadata(dest)
    assert (meta.width, meta.height) == (720, 1280)
    assert meta.fps == 60
    assert meta.audio_codec == 'aac'
    assert .48 <= meta.duration <= .6


def test_speech_subtracts_sorted_overlapping_silences():
    from app.services.video_analyzer import LocalVideoAnalyzer
    silences = [{'start': 8, 'end': 12}, {'start': -2, 'end': 3},
                {'start': 2, 'end': 4}, {'start': 1, 'end': 2}]
    assert LocalVideoAnalyzer.speech_from_silences(silences, 10) == [{'start': 4, 'end': 8}]
    assert LocalVideoAnalyzer.speech_from_silences(silences, 10, [(3, 9)]) == [{'start': 4, 'end': 8}]


def test_late_tracker_detection_does_not_snap_camera_mid_shot(tmp_path):
    path = tmp_path / 'camera.txt'
    tracks = [{'time': t, 'all_faces': [{'x': .7, 'w': .1, 'track_id': 1}]}
              for t in [.5, 1, 1.5]]
    CropSmoothingService.generate_shot_sendcmd({'start': 0, 'end': 2}, tracks,
                                              1920, 608, path, fps=60)
    positions = [int(line.split()[-1].rstrip(';')) for line in path.read_text().splitlines()]
    assert len(set(positions)) == 1
    assert positions[0] > 1000


def test_captions_have_color_without_gemini_emphasis(tmp_path):
    from PIL import Image
    data = FFmpegService._create_subtitle_images([{
        "start_seconds": 0, "end_seconds": 1.5,
        "text": "He gave away $10000 today"}], tmp_path, 2, 1080, "color-fallback")
    image = Image.open(data["images"][1]["path"]).convert("RGBA")
    colors = set(image.getdata())
    assert (255, 216, 102, 255) in colors
    assert (255, 255, 255, 255) in colors
    assert image.getbbox()[0] >= 70
    assert image.getbbox()[2] <= 1010
    playlist = data["concat_path"].read_text() if hasattr(data["concat_path"], "read_text") else Path(data["concat_path"]).read_text()
    assert "duration 1.500" in playlist
