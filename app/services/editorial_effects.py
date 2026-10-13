"""Validate semantic camera effects against the final, ordered edit timeline."""

import math

from app.config import config
from app.models.schemas import ClipCandidate
from app.services.timeline import with_output_times

EFFECT_TYPES = {"PUNCH_IN", "PUNCH_OUT", "REACTION_PUSH", "SLOW_PUSH", "IMPACT"}
STYLE_BUDGETS = {
    "CLEAN_VIRAL": 2, "PODCAST_DYNAMIC": 3, "COMEDY_TIMING": 2,
    "STORY_DRIVEN": 1, "EDUCATIONAL_FAST": 3, "ACTION_ENERGY": 4,
    "EMOTIONAL_MINIMAL": 0, "PRODUCT_DEMO": 2, "GAMEPLAY_DYNAMIC": 3,
}


def style_for(candidate: ClipCandidate) -> str:
    content = candidate.content_type.upper()
    mode = candidate.pacing_mode.upper()
    if mode == "EMOTIONAL" or content in {"EMOTIONAL", "EMOTIONAL_STORY"}:
        return "EMOTIONAL_MINIMAL"
    if mode == "COMEDIC" or content in {"COMEDY", "STANDUP"}:
        return "COMEDY_TIMING"
    if content in {"PODCAST", "INTERVIEW", "CONVERSATION", "LATE_NIGHT"}:
        return "PODCAST_DYNAMIC"
    if content == "GAMEPLAY":
        return "GAMEPLAY_DYNAMIC"
    if content in {"PRODUCT_DEMO", "DEMONSTRATION"}:
        return "PRODUCT_DEMO"
    if content in {"SPORT", "SPORTS", "ACTION"}:
        return "ACTION_ENERGY"
    if mode == "EDUCATIONAL" or content in {"TUTORIAL", "EDUCATIONAL", "PRODUCT_DEMO"}:
        return "EDUCATIONAL_FAST"
    if mode in {"STORY", "TENSION"} or content == "STORY":
        return "STORY_DRIVEN"
    return "CLEAN_VIRAL"


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def plan_effects(candidate: ClipCandidate, shots: list[dict],
                 scene_cuts: list[float]) -> tuple[list[dict], dict]:
    """Keep only justified effects wholly inside a real source shot."""
    mapped = with_output_times(shots)
    for shot in mapped:
        # Legacy semantic zoom has no confidence or precise bounds. Production
        # uses only validated effect_events, not the old whole-shot zoom.
        shot["zoom_intent"] = "NONE"
    style = style_for(candidate)
    candidate.style_profile = style
    total = sum(shot["end"] - shot["start"] for shot in mapped)
    budget = min(STYLE_BUDGETS[style], max(0, int(total // 10)))
    # Original cuts already supply visual variation.
    if len(scene_cuts) > total / 5:
        budget = max(0, budget - 2)
    candidate.effect_budget = budget
    valid = []
    for effect in candidate.effect_events[:24]:
        if effect.effect_type not in EFFECT_TYPES or not effect.reason_code.strip():
            continue
        if not all(_finite(x) for x in (effect.start_seconds, effect.end_seconds,
                                       effect.confidence, effect.intensity)):
            continue
        if not 0 <= effect.confidence <= 1 or not 0 < effect.intensity <= .12:
            continue
        if effect.confidence < config.EFFECT_MIN_CONFIDENCE:
            continue
        if not .2 <= effect.end_seconds - effect.start_seconds <= 3:
            continue
        if any(abs(cut - effect.start_seconds) < .25 or
               abs(cut - effect.end_seconds) < .25 for cut in scene_cuts):
            continue
        overlay = candidate.context_overlay
        if overlay and (abs(overlay.start_seconds - effect.start_seconds) < .3 or
                        abs(overlay.end_seconds - effect.start_seconds) < .3):
            continue
        if any(pause.start_seconds < effect.end_seconds and
               pause.end_seconds > effect.start_seconds
               for pause in candidate.preserve_pauses):
            continue
        if candidate.importance_curve:
            nearby = [point for point in candidate.importance_curve
                      if effect.start_seconds - .75 <= point.source_time <=
                      effect.end_seconds + .75]
            if nearby and max(max(point.semantic_importance,
                                  point.emotional_intensity,
                                  point.reaction_strength, point.surprise,
                                  point.comedic_value) for point in nearby) < 5:
                continue
        shot_index = next((index for index, shot in enumerate(mapped)
                           if shot["start"] <= effect.start_seconds and
                           effect.end_seconds <= shot["end"] and
                           shot.get("composition", "medium").lower()
                           not in {"wide", "fit_with_background", "original", "close"}), None)
        if shot_index is None:
            continue
        if effect.effect_type == "PUNCH_OUT" and \
                abs(effect.start_seconds - mapped[shot_index]["start"]) > .05:
            continue
        valid.append((effect.confidence, effect, shot_index))
    # A single optional effect per shot, with breathing room between shots.
    valid.sort(key=lambda row: row[0], reverse=True)
    chosen = []
    for _, effect, index in valid:
        if len(chosen) >= budget:
            break
        if any(i == index or abs(effect.start_seconds - old.start_seconds) < 1.5 or
               (effect.effect_type == old.effect_type and
                abs(effect.start_seconds - old.start_seconds) < 5)
               for old, i in chosen):
            continue
        chosen.append((effect, index))
    overediting = max(0, len(valid) - budget) + sum(
        1 for a, b in zip(sorted(valid, key=lambda row: row[1].start_seconds),
                        sorted(valid, key=lambda row: row[1].start_seconds)[1:])
        if b[1].start_seconds - a[1].end_seconds < .5)
    candidate.overediting_score = float(overediting)
    events = []
    for effect, index in chosen:
        shot = mapped[index]
        item = {"effect_type": effect.effect_type,
                "source_start": effect.start_seconds,
                "source_end": effect.end_seconds,
                "final_start": shot["output_start"] + effect.start_seconds - shot["start"],
                "final_end": shot["output_start"] + effect.end_seconds - shot["start"],
                "reason_code": effect.reason_code[:64],
                "confidence": effect.confidence,
                "intensity": min(.10, effect.intensity)}
        shot["effect"] = item
        events.append(item)
    return mapped, {"style_profile": style, "effect_budget": budget,
                    "overediting_score": candidate.overediting_score,
                    "effect_events": sorted(events, key=lambda item: item["final_start"])}


def validate_edit_plan(candidate: ClipCandidate, shots: list[dict],
                       effects: list[dict], source_duration: float) -> str | None:
    """Cheap structural gate before rendering or transcription."""
    if not shots:
        return "EMPTY_SHOT_PLAN"
    cursor = 0.0
    for shot in shots:
        if not all(_finite(shot.get(key)) for key in
                   ("start", "end", "output_start", "output_end")):
            return "INVALID_SHOT_TIME"
        if shot["start"] < 0 or shot["end"] > source_duration + .05 or \
                shot["end"] - shot["start"] < .025:
            return "INVALID_SHOT_BOUNDS"
        if abs(shot["output_start"] - cursor) > .01:
            return "TIMELINE_GAP_OR_OVERLAP"
        cursor = shot["output_end"]
    expected = sum(s.end_seconds - s.start_seconds for s in candidate.source_segments)
    if abs(cursor - expected) > .1:
        return "EDL_DURATION_MISMATCH"
    shot_index = 0
    for segment in candidate.source_segments:
        source_cursor = segment.start_seconds
        while shot_index < len(shots) and source_cursor < segment.end_seconds - .02:
            shot = shots[shot_index]
            if abs(shot["start"] - source_cursor) > .03 or \
                    shot["end"] > segment.end_seconds + .03:
                return "SHOT_OUTSIDE_EDL"
            source_cursor = shot["end"]
            shot_index += 1
        if abs(source_cursor - segment.end_seconds) > .03:
            return "SHOT_OUTSIDE_EDL"
    if shot_index != len(shots):
        return "SHOT_OUTSIDE_EDL"
    if candidate.payoff_timestamp is not None and not any(
            shot["start"] <= candidate.payoff_timestamp < shot["end"]
            for shot in shots):
        return "PAYOFF_MISSING"
    for effect in effects:
        if not 0 <= effect["final_start"] < effect["final_end"] <= cursor:
            return "EFFECT_OUTSIDE_TIMELINE"
    return None
