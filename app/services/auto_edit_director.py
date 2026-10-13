"""Coordinate existing editorial intelligence; never render or publish media."""

from app.config import config
from app.models.schemas import RenderSettings
from app.services.editorial_service import prepare_candidates, validate_final_edl
from app.services.editorial_effects import plan_effects, validate_edit_plan
from app.services.visual_director import VisualDirector, VirtualShot
from app.services.timeline import remap_intervals

AUTO_EDIT_VERSION = "auto-edit-v1"


class AutoEditDirector:
    @staticmethod
    def settings(guidance: str | None = None, target_count: int | None = None, count_mode: str = "AUTO") -> RenderSettings:
        if count_mode not in {"AUTO", "TARGET"}:
            raise ValueError("INVALID_CLIP_COUNT")
        if count_mode == "AUTO":
            target_count = None
        elif type(target_count) is not int or target_count not in {1, 2, 3, 5, 10}:
            raise ValueError("INVALID_CLIP_COUNT")
        # Count is a discovery ceiling, never a target filled with time ranges.
        if count_mode == "TARGET":
            if target_count == 1: discovery_count = 8
            elif target_count == 2: discovery_count = 8
            elif target_count == 3: discovery_count = 10
            elif target_count == 5: discovery_count = 12
            elif target_count == 10: discovery_count = 20
            else: discovery_count = max(8, (target_count or 0) * 2)
        else:
            discovery_count = max(config.AUTO_EDIT_MAX_CANDIDATES, (target_count or 0) + 3)
        return RenderSettings(auto_edit=True, clip_count=discovery_count,
                              target_clip_count=target_count, count_mode=count_mode,
                              exact_clip_count=False, min_clip_seconds=5,
                              max_clip_seconds=60, subtitles=True,
                              output_resolution="1080x1920", editorial_guidance=guidance)

    @staticmethod
    def understand(service, video_file, settings: dict):
        # GeminiService owns the source-map/discovery/deep/critic passes; reuse its
        # uploaded file and bounded failure handling rather than a second pipeline.
        analysis_result = service.analyze_video(video_file, {**settings, "auto_edit": True,
                                                             "exact_clip_count": False})
        return AutoEditDirector.compress(service, video_file, analysis_result, settings)

    @staticmethod
    def needs_compression(analysis, settings):
        ceiling = min(60, settings.get("max_clip_seconds", 60))
        return any(sum(s.end_seconds - s.start_seconds for s in c.source_segments) > ceiling
                   for c in analysis.clips)

    @staticmethod
    def compress(service, video_file, analysis, settings):
        from app.services.gemini_requests import transient_failure, failure_category
        from app.services.candidate_compression import validate_compression
        for candidate in analysis.clips:
            original_length = sum(s.end_seconds - s.start_seconds for s in candidate.source_segments)
            if original_length <= min(60, settings.get("max_clip_seconds", 60)):
                continue
            if not candidate.semantic_source_segments:
                candidate.semantic_source_segments = [s.model_copy(deep=True) for s in candidate.source_segments]
            candidate.compression_attempted = True
            candidate.duration_before_compression = original_length
            candidate.proposed_compressed_segments = []
            candidate.duration_after_compression = None
            try:
                proposed = service.compress_candidate(video_file, candidate.model_copy(deep=True), settings)
                candidate.proposed_compressed_segments = [s.model_copy(deep=True) for s in proposed]
                error = validate_compression(candidate, proposed, settings)
                if error:
                    candidate.compression_status = "FAILED"
                    candidate.compression_error_code = error
                    continue
            except Exception as exc:
                candidate.compression_status = "SERVICE_UNAVAILABLE" if transient_failure(exc) else "FAILED"
                candidate.compression_error_code = failure_category(exc) if transient_failure(exc) else "COMPRESSION_REQUEST_FAILED"
                continue
            candidate.source_segments = proposed
            candidate.duration_after_compression = sum(s.end_seconds - s.start_seconds for s in proposed)
            candidate.compression_status = "SUCCESS"
            candidate.compression_error_code = None
            # An older selection must not poison a successfully repaired EDL.
            if candidate.hard_rejection_reason == "UNUSABLE_DURATION":
                candidate.hard_rejection_reason = None
            candidate.decision_trace.append(
                f"Compressed from {original_length:.1f}s to {candidate.duration_after_compression:.1f}s")
        return analysis

    @staticmethod
    def select(analysis, duration: float, signals: dict, target_count: int | None = None):
        audit = []
        decisions = []
        candidates = prepare_candidates(analysis.clips, duration, signals,
                                        analysis.events, analysis.narrative_relationships, audit, decisions, count_mode="TARGET" if target_count is not None else "AUTO")

        # 'candidates' contains only structurally valid, distinct clips (marked SELECTED).
        # We handle the AUTO vs TARGET mode truncation here.
        selected = []
        for candidate in candidates:
            if target_count is None and "BELOW_MIN_SCORE" in candidate.quality_flags:
                # In AUTO mode, we strictly drop items below quality floor.
                # Since prepare_candidates already marked it SELECTED, we need to correct the decision for the audit log.
                candidate.hard_rejection_reason = "AUTO_MODE_QUALITY_FLOOR"
                for d in decisions:
                    if d.get("title") == candidate.title and d.get("decision") == "SELECTED":
                        d["decision"] = "NOT_SELECTED"
                continue
            selected.append(candidate)

        quality_candidate_count = len(selected)
        max_allowed = target_count if target_count is not None else config.AUTO_EDIT_MAX_CANDIDATES

        # Any candidates that fall outside max_allowed are marked NOT_SELECTED.
        final_selected = selected[:max_allowed]
        dropped = selected[max_allowed:]
        for candidate in dropped:
            candidate.hard_rejection_reason = "NOT_SELECTED"
            for d in decisions:
                if d.get("title") == candidate.title and d.get("decision") == "SELECTED":
                    d["decision"] = "NOT_SELECTED"

        for rank, candidate in enumerate(final_selected, 1):
            candidate.rank = rank

        return final_selected, audit, quality_candidate_count, decisions

    @staticmethod
    def plan(candidate, source_duration: float, signals: dict) -> tuple[list[dict], dict]:
        error = validate_final_edl(candidate, source_duration)
        if error:
            raise ValueError(error)
        director = VisualDirector.for_pacing_mode(candidate.pacing_mode)
        shots = director.build_shot_plan(candidate, signals.get("tracks", []),
                                         signals.get("speech", []), signals.get("scenes", []))
        if not shots:
            shots = [VirtualShot(s.start_seconds, s.end_seconds, [], "fit_with_background")
                     for s in candidate.source_segments]
        mapped, plan = plan_effects(candidate, [shot.to_dict() for shot in shots],
                                    signals.get("scenes", []))
        error = validate_edit_plan(candidate, mapped, plan["effect_events"], source_duration)
        if error:
            raise ValueError(error)
        beats = remap_intervals(mapped, [beat.model_dump() for beat in candidate.micro_beats])
        retention = []
        for beat in beats:
            kind = beat["kind"].upper()
            label = ("REACTION" if kind in {"REACTION", "SECOND_REACTION", "LAUGHTER"} else
                     "PAYOFF" if kind in {"PAYOFF", "REVEAL", "RESOLUTION", "BUTTON", "PUNCHLINE"} else
                     "PEAK" if kind in {"SURPRISE", "ESCALATION"} else
                     "WEAK" if kind in {"REPETITION", "FILLER", "DEAD_TIME"} else "BUILDING")
            retention.append({"start": beat["start_seconds"], "end": beat["end_seconds"],
                              "label": label, "evidence": beat.get("evidence", "")[:180]})
        plan.update({"auto_edit_version": AUTO_EDIT_VERSION,
                     "micro_beats": beats, "retention_map": retention,
                     "retention_risks": {key: getattr(candidate.scores, key) for key in
                         ("early_swipe_risk", "confusion_risk", "dead_time_penalty",
                          "repetition_penalty", "weak_ending_penalty")},
                     "decision_trace": candidate.decision_trace[:40],
                     "camera_events": [{"start": s["output_start"], "end": s["output_end"],
                                        "composition": s["composition"], "subject": s["subject"]}
                                       for s in mapped],
                     "deep_analysis_status": candidate.deep_analysis_status,
                     "preserve_pauses": remap_intervals(mapped, [s.model_dump() for s in candidate.preserve_pauses]),
                     "context_overlays": remap_intervals(mapped, [candidate.context_overlay.model_dump()])
                                         if candidate.context_overlay else []})
        return mapped, plan
