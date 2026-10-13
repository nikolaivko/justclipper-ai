"""Deterministic editorial QC of Gemini's source-time EDL proposals."""

from __future__ import annotations

import json

from app.models.schemas import ClipCandidate, EDLSegment, NarrativeEvent, NarrativeRelationship
from app.config import config

EDITOR_VERSION = "auto-editor-v4"

POSITIVE_WEIGHTS = {
    "hook_strength": 1.1, "curiosity_gap": 0.6,
    "first_1s_strength": 2.0, "first_3s_strength": 1.7,
    "first_5s_strength": 1.0, "mid_clip_retention": 0.8,
    "payoff_retention": 0.9, "ending_retention": 0.9,
    "standalone_clarity": 1.5, "payoff_strength": 2.0,
    "ending_strength": 1.5, "reaction_value": 0.8,
    "story_value": 0.8, "visual_interest": 0.6,
    "humor_strength": 0.6, "information_value": 0.6,
    "emotional_strength": 0.6, "editability": 0.6,
    "rewatch_potential": 0.4, "social_shareability": 0.4,
    "surprise": 0.3, "tension": 0.2, "conflict": 0.2,
    "visual_change": 0.2, "motion_energy": 0.1,
    "relatability": 0.2, "comment_potential": 0.2,
}
PENALTY_WEIGHTS = {
    "context_cost": 1.2, "dead_time_penalty": 1.0,
    "redundancy_penalty": 0.8, "early_swipe_risk": 1.4,
    "confusion_risk": 1.5, "slow_start_penalty": 1.2,
    "dead_air_penalty": 0.8, "repetition_penalty": 0.8,
    "weak_ending_penalty": 1.6,
}
PAUSE_MODES = {"COMEDIC", "TENSION", "EMOTIONAL"}
PAUSE_REASONS = {"PRESERVE_PAUSE_FOR_COMEDY", "PRESERVE_PAUSE_FOR_TENSION",
                 "PRESERVE_PAUSE_FOR_REACTION", "PRESERVE_PAUSE_FOR_EMOTION"}


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end


def _contains(segments: list[EDLSegment], when: float, margin: float = 0.05) -> bool:
    return any(s.start_seconds - margin <= when <= s.end_seconds + margin for s in segments)


def _contains_payoff(segments: list[EDLSegment], when: float) -> bool:
    return any(s.start_seconds <= when <= s.end_seconds - .2 for s in segments)


def _normalize(segments: list[EDLSegment], duration: float) -> list[EDLSegment]:
    result = []
    import math
    for segment in segments:
        if not math.isfinite(segment.start_seconds) or not math.isfinite(segment.end_seconds):
            continue
        start = max(0.0, min(duration, segment.start_seconds))
        end = max(0.0, min(duration, segment.end_seconds))
        if end - start >= 0.25:
            result.append(EDLSegment(start_seconds=start, end_seconds=end,
                                     reason=segment.reason))
    return result


def _context_from_graph(candidate: ClipCandidate, events: list[NarrativeEvent],
                        relations: list[NarrativeRelationship]) -> list[EDLSegment]:
    if candidate.payoff_timestamp is None:
        return []
    by_id = {event.event_id: event for event in events}
    payoffs = {event.event_id for event in events if
               event.start_seconds <= candidate.payoff_timestamp <= event.end_seconds}
    required_ids = {relation.from_event_id for relation in relations
                    if relation.to_event_id in payoffs}
    for payoff_id in payoffs:
        required_ids.update(by_id[payoff_id].required_context_event_ids)
    return [EDLSegment(start_seconds=event.start_seconds,
                       end_seconds=event.end_seconds,
                       reason="Required narrative context")
            for event_id in sorted(required_ids)
            if (event := by_id.get(event_id)) is not None]


def _insert_context(candidate: ClipCandidate, context: list[EDLSegment], duration: float) -> None:
    missing = []
    for interval in sorted(_normalize(context, duration),
                           key=lambda item: item.start_seconds):
        midpoint = (interval.start_seconds + interval.end_seconds) / 2
        if _contains(candidate.source_segments + missing, midpoint):
            continue
        missing.append(interval)
        candidate.decision_trace.append("Included required setup from earlier source time")
    cold_open_before_payoff = (candidate.hook_strategy != "NORMAL_CHRONOLOGICAL" and
                               candidate.source_segments and
                               (candidate.payoff_timestamp is None or
                                not _contains(candidate.source_segments[:1],
                                              candidate.payoff_timestamp)))
    if cold_open_before_payoff:
        candidate.source_segments[1:1] = missing
    else:
        candidate.source_segments = sorted(candidate.source_segments + missing,
                                           key=lambda item: item.start_seconds)


def _append_reaction(candidate: ClipCandidate, duration: float) -> None:
    if not candidate.source_segments:
        return
    if candidate.reaction_window and candidate.reaction_window not in candidate.reaction_moments:
        candidate.reaction_moments.append(candidate.reaction_window)
    last_end = candidate.source_segments[-1].end_seconds
    for reaction in sorted(_normalize(candidate.reaction_moments, duration),
                           key=lambda item: item.start_seconds):
        if (last_end <= reaction.start_seconds <= last_end + 3.0 and
                not _contains(candidate.source_segments,
                              (reaction.start_seconds + reaction.end_seconds) / 2)):
            candidate.source_segments.append(EDLSegment(
                start_seconds=reaction.start_seconds,
                end_seconds=reaction.end_seconds, reason="Preserve reaction after payoff"))
            candidate.decision_trace.append("Preserved reaction after payoff")
            last_end = reaction.end_seconds


def _trim_dead_air(candidate: ClipCandidate, silences: list[dict]) -> None:
    if not silences:
        return
    result = []
    for segment in candidate.source_segments:
        cursor = segment.start_seconds
        for silence in sorted(silences, key=lambda item: item["start"]):
            begin, end = silence["start"], silence["end"]
            if begin < cursor or end > segment.end_seconds or end - begin < 1.5:
                continue
            protected = any(_overlaps(begin, end, pause.start_seconds,
                                      pause.end_seconds) for pause in candidate.preserve_pauses)
            protected |= any(_overlaps(begin, end, reaction.start_seconds,
                                       reaction.end_seconds) for reaction in candidate.reaction_moments)
            protected |= (candidate.payoff_timestamp is not None and
                          begin <= candidate.payoff_timestamp <= end)
            if protected or candidate.pacing_mode in PAUSE_MODES:
                if protected:
                    candidate.decision_trace.append("Preserved meaningful pause or reaction")
                continue
            keep_end = cursor if begin <= cursor + .05 else min(end, begin + 0.4)
            if keep_end - cursor >= 0.25:
                result.append(EDLSegment(start_seconds=cursor, end_seconds=keep_end,
                                         reason=segment.reason or "Preserve spoken beat"))
            candidate.decision_trace.append("Removed irrelevant silence")
            cursor = end
        if segment.end_seconds - cursor >= 0.25:
            result.append(EDLSegment(start_seconds=cursor,
                                     end_seconds=segment.end_seconds, reason=segment.reason))
    if result:
        candidate.source_segments = result


def score_candidate(candidate: ClipCandidate) -> float:
    scores = candidate.scores
    available = any(name in scores.model_fields_set for name in POSITIVE_WEIGHTS)
    if not available:
        # Older saved Gemini responses have only the original scalar fields.
        positives = [candidate.hook_strength, candidate.standalone_clarity,
                     candidate.payoff_strength, candidate.reaction_value,
                     candidate.story_value, candidate.visual_interest]
        base = sum(value for value in positives if value is not None)
        divisor = max(1, sum(value is not None for value in positives))
        return round(max(0, min(100, base / divisor * 10)), 1)
    weights = dict(POSITIVE_WEIGHTS)
    try:
        overrides = json.loads(config.EDITORIAL_SCORE_WEIGHTS or "{}")
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if key in weights and isinstance(value, (int, float)) and 0 <= value <= 10:
                    weights[key] = float(value)
    except (ValueError, TypeError):
        pass
    supplied = {key: weight for key, weight in weights.items()
                if key in scores.model_fields_set}
    weighted = sum(min(10, max(0, getattr(scores, key))) * weight
                   for key, weight in supplied.items())
    maximum = max(1, sum(supplied.values()) * 10)
    penalties = {key: weight for key, weight in PENALTY_WEIGHTS.items()
                 if key in scores.model_fields_set}
    penalty = sum(min(10, max(0, getattr(scores, key))) * weight
                  for key, weight in penalties.items())
    penalty_scale = max(1, sum(penalties.values()) * 10)
    score = weighted / maximum * 100 - penalty / penalty_scale * 45
    if candidate.payoff_present is False and not candidate.intentional_unresolved_ending:
        score -= 45
    if candidate.resolution_present is False and not candidate.intentional_unresolved_ending:
        score -= 20
    return round(max(0, min(100, score)), 1)


def _duplicate(candidate: ClipCandidate, chosen: ClipCandidate) -> bool:
    a = candidate.source_segments
    b = chosen.source_segments
    a_duration = sum(s.end_seconds - s.start_seconds for s in a)
    b_duration = sum(s.end_seconds - s.start_seconds for s in b)
    overlap = sum(max(0.0, min(x.end_seconds, y.end_seconds) -
                      max(x.start_seconds, y.start_seconds)) for x in a for y in b)
    same_payoff = (candidate.payoff_timestamp is not None and
                   chosen.payoff_timestamp is not None and
                   abs(candidate.payoff_timestamp - chosen.payoff_timestamp) <= 3)
    candidate_story = (candidate.story_arc.payoff if candidate.story_arc else "").strip().casefold()
    chosen_story = (chosen.story_arc.payoff if chosen.story_arc else "").strip().casefold()
    same_story = (len(candidate_story) >= 12 and candidate_story == chosen_story and
                  candidate.topic.casefold() == chosen.topic.casefold())
    return (overlap >= 0.65 * min(a_duration, b_duration) or
            same_payoff and candidate.topic.casefold() == chosen.topic.casefold() or
            same_story)


def prepare_candidates(candidates: list[ClipCandidate], duration: float,
                       analysis_data: dict | None = None,
                       events: list[NarrativeEvent] | None = None,
                       relationships: list[NarrativeRelationship] | None = None,
                       audit: list[str] | None = None,
                       decisions: list[dict] | None = None,
                       count_mode: str = "AUTO"
                       ) -> list[ClipCandidate]:
    analysis_data = analysis_data or {}
    prepared = []
    records = {}
    def record(candidate, reason):
        if audit is not None:
            audit.append(reason)
        if decisions is not None:
            records[id(candidate)]["decision"] = reason
            records[id(candidate)]["editorial_score"] = candidate.editorial_score
            records[id(candidate)].update(candidate.model_dump(include={
                "deep_pass_status", "deep_pass_error_code", "critic_status", "critic_error_code",
                "critic_issue", "compression_attempted", "compression_status", "compression_error_code",
                "semantic_source_segments", "proposed_compressed_segments", "duration_before_compression",
                "duration_after_compression", "quality_flags", "hard_rejection_reason"}))
    for candidate in candidates:
        if decisions is not None:
            item = {"rank": candidate.rank, "title": candidate.title,
                    "source_segments": [s.model_dump() for s in candidate.source_segments],
                    "scores": candidate.scores.model_dump(exclude_unset=True),
                    "score_floor": config.EDITORIAL_MIN_SCORE,
                    "deep_analysis_status": candidate.deep_analysis_status}
            decisions.append(item)
            records[id(candidate)] = item
        if (candidate.compression_status == "SERVICE_UNAVAILABLE" and
                not validate_final_edl(candidate, duration)):
            candidate.editorial_score = score_candidate(candidate)
            candidate.hard_rejection_reason = None
            record(candidate, "COMPRESSION_SERVICE_UNAVAILABLE")
            continue
        candidate.source_segments = _normalize(candidate.source_segments, duration)
        candidate.decision_trace = []
        # Broad and focused Gemini passes can disagree. Keep only observations
        # that intersect this candidate and have valid absolute source times.
        candidate.micro_beats = [beat for beat in candidate.micro_beats[:60]
                                 if 0 <= beat.start_seconds < beat.end_seconds <= duration
                                 and _contains(candidate.source_segments,
                                               (beat.start_seconds + beat.end_seconds) / 2)]
        candidate.importance_curve = [point for point in candidate.importance_curve[:80]
                                      if 0 <= point.source_time <= duration and
                                      _contains(candidate.source_segments, point.source_time)]
        # Hook variants may reorder only the same source material. This avoids
        # dropping a setup or silently adding a second version of a quote.
        baseline = sorted((round(s.start_seconds, 2), round(s.end_seconds, 2))
                          for s in candidate.source_segments)
        viable = []
        for variant in candidate.hook_variants[:4]:
            proposed = _normalize(variant.source_segments, duration)
            coverage = sorted((round(s.start_seconds, 2), round(s.end_seconds, 2))
                              for s in proposed)
            if (coverage == baseline and
                    (candidate.payoff_timestamp is None or
                     (proposed and _contains_payoff(proposed[-1:],
                                                    candidate.payoff_timestamp))) and
                    variant.misleading_risk <= 2 and
                    variant.continuity >= 7 and variant.payoff_preservation >= 7):
                value = (variant.first_1s * 2 + variant.first_3s +
                         variant.clarity * 1.5 + variant.curiosity -
                         variant.misleading_risk * 2)
                viable.append((value, variant))
        if viable:
            _, best = max(viable, key=lambda pair: pair[0])
            if best.strategy != candidate.hook_strategy:
                candidate.source_segments = best.source_segments
                candidate.hook_strategy = best.strategy
                candidate.decision_trace.append("Selected truthful hook variant")
        for region in (candidate.removed_regions or [])[:20]:
            candidate.decision_trace.append(
                f"Removed {region.start_seconds:.1f}-{region.end_seconds:.1f}: "
                f"{(region.reason or 'redundant material')[:120]}")
        for pause in candidate.preserve_pauses[:20]:
            candidate.decision_trace.append(
                f"Preserved {pause.start_seconds:.1f}-{pause.end_seconds:.1f}: "
                f"{(pause.reason or 'editorial timing')[:120]}")
        if not candidate.source_segments:
            record(candidate, "NO_VALID_SEGMENTS")
            continue
        context = list(candidate.required_context)
        context += _context_from_graph(candidate, events or [], relationships or [])
        candidate.required_context = _normalize(context, duration)
        _insert_context(candidate, context, duration)
        _append_reaction(candidate, duration)
        overlay = candidate.context_overlay
        if overlay:
            evidence = next((event for event in events or []
                             if event.event_id == overlay.evidence_event_id), None)
            if (evidence is None or len(overlay.text) > 60 or
                    overlay.end_seconds - overlay.start_seconds > 4 or
                    overlay.start_seconds < 0 or overlay.end_seconds > duration or
                    overlay.end_seconds <= overlay.start_seconds or
                    not _contains(candidate.source_segments,
                                  (overlay.start_seconds + overlay.end_seconds) / 2)):
                candidate.context_overlay = None
                candidate.decision_trace.append("Discarded unsupported context text")
            else:
                candidate.decision_trace.append(
                    f"Context text supported by event {evidence.event_id}")
        for cut in candidate.cut_suggestions[:20]:
            if 0 <= cut.time <= duration and cut.confidence >= .7:
                candidate.decision_trace.append(
                    f"Cut {cut.time:.2f}: {cut.type[:32]} — {cut.reason[:120]}")
        if candidate.payoff_present is False and not candidate.intentional_unresolved_ending:
            candidate.decision_trace.append("Flagged: no payoff")
            candidate.quality_flags.append("NO_PAYOFF")
        if candidate.resolution_present is False and not candidate.intentional_unresolved_ending:
            candidate.decision_trace.append("Flagged: no resolution")
            candidate.quality_flags.append("NO_RESOLUTION")
        if (candidate.ending_quality is not None and candidate.ending_quality < 3 and
                not candidate.intentional_unresolved_ending):
            candidate.decision_trace.append("Flagged: weak ending")
            candidate.quality_flags.append("WEAK_ENDING")
        if (candidate.payoff_timestamp is not None and
                not _contains_payoff(candidate.source_segments,
                                     candidate.payoff_timestamp)):
            candidate.decision_trace.append("Rejected: payoff omitted from EDL")
            candidate.hard_rejection_reason = "PAYOFF_OUTSIDE_EDL"
        cut_risk = any(
            (cut.semantic_continuity < 5 or cut.misleading_risk > 5) and any(
                abs(left.end_seconds - cut.from_source_end) < .25 and
                abs(right.start_seconds - cut.to_source_start) < .25
                for left, right in zip(candidate.source_segments,
                                       candidate.source_segments[1:]))
            for cut in candidate.cut_continuity)
        if cut_risk:
            candidate.decision_trace.append("Flagged: misleading or incoherent cut")
            candidate.quality_flags.append("CUT_CONTINUITY_RISK")
        _trim_dead_air(candidate, analysis_data.get("silences", []))
        if (candidate.payoff_timestamp is not None and
                not _contains_payoff(candidate.source_segments,
                                     candidate.payoff_timestamp)):
            candidate.hard_rejection_reason = "PAYOFF_REMOVED_BY_TRIM"
        candidate.editorial_score = score_candidate(candidate)
        if candidate.critic_issue in {"MISLEADING", "MISSING_PAYOFF", "UNCLEAR_OPENING"}:
            candidate.editorial_score = max(0, candidate.editorial_score - 15)
        candidate.overall_score = round(candidate.editorial_score / 10)
        from app.services.editorial_effects import style_for
        candidate.style_profile = style_for(candidate)
        candidate.decision_trace.append(f"Pacing: {candidate.pacing_mode}")
        candidate.decision_trace.append(f"Opening: {candidate.hook_strategy}")
        candidate.decision_trace.append(f"Editorial score: {candidate.editorial_score:.1f}")
        prepared.append(candidate)
    chosen = []
    if prepared:
        low = min(item.editorial_score or 0 for item in prepared)
        high = max(item.editorial_score or 0 for item in prepared)
        for candidate in prepared:
            candidate.relative_quality = round(
                100 * ((candidate.editorial_score or 0) - low) / max(1, high - low), 1)
    for candidate in sorted(prepared, key=lambda item: item.editorial_score or 0, reverse=True):
        error = validate_final_edl(candidate, duration)
        if error:
            # TARGET mode lenient payoff validation
            if error == "PAYOFF_MISSING" and candidate.deep_analysis_status in {None, "NOT_REQUESTED", "BROAD_FALLBACK"} and count_mode == "TARGET":
                candidate.quality_flags.append("NO_PAYOFF")
                candidate.decision_trace.append("Flagged: payoff missing but preserved for deep/target mode")
            else:
                candidate.hard_rejection_reason = error
        elif candidate.selection_origin != "GEMINI":
            candidate.hard_rejection_reason = "INVALID_ORIGIN"
        else:
            length = sum(s.end_seconds - s.start_seconds for s in candidate.source_segments)
            if not 5 <= length <= 60:
                if candidate.compression_status == "SERVICE_UNAVAILABLE":
                    candidate.decision_trace.append("Flagged: unusable duration but compression is retryable")
                    candidate.quality_flags.append("COMPRESSION_RETRYABLE")
                else:
                    candidate.hard_rejection_reason = "UNUSABLE_DURATION" 

        if (candidate.editorial_score or 0) < config.EDITORIAL_MIN_SCORE:
            candidate.quality_flags.append("BELOW_MIN_SCORE")

        if candidate.hard_rejection_reason:
            record(candidate, candidate.hard_rejection_reason)
        elif any(_duplicate(candidate, other) for other in chosen):
            record(candidate, "DUPLICATE")
        else:
            chosen.append(candidate)
            record(candidate, "SELECTED")
    for rank, candidate in enumerate(chosen, 1):
        candidate.rank = rank
    return chosen


def validate_final_edl(candidate: ClipCandidate, source_duration: float) -> str | None:
    """Reject a user-edited EDL that loses its payoff or required setup."""
    if not candidate.source_segments:
        return "EMPTY_EDL"
    import math
    for segment in candidate.source_segments:
        if (not math.isfinite(segment.start_seconds) or
                not math.isfinite(segment.end_seconds) or segment.start_seconds < 0 or (source_duration > 0 and
                segment.end_seconds > source_duration) or
                segment.end_seconds - segment.start_seconds < 0.25):
            return "INVALID_EDL_SEGMENT"
    if not candidate.intentional_unresolved_ending:
        if candidate.payoff_present is False:
            return "PAYOFF_MISSING"
        if candidate.payoff_present is None and candidate.payoff_timestamp is None:
            return "PAYOFF_MISSING"
    if (candidate.payoff_timestamp is not None and
            not _contains_payoff(candidate.source_segments,
                                 candidate.payoff_timestamp)):
        return "PAYOFF_MISSING"
    for context in candidate.required_context:
        middle = (context.start_seconds + context.end_seconds) / 2
        if not _contains(candidate.source_segments, middle):
            return "REQUIRED_CONTEXT_MISSING"
    if candidate.context_overlay:
        overlay = candidate.context_overlay
        midpoint = (overlay.start_seconds + overlay.end_seconds) / 2
        if not _contains(candidate.source_segments, midpoint):
            return "CONTEXT_OVERLAY_OUTSIDE_EDL"
    return None
