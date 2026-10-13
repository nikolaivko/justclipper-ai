"""Check AI source coordinates before editorial scoring; never guess time units."""
import math


class InvalidSemanticTimeline(ValueError):
    def __init__(self, audit):
        super().__init__("AI_INVALID_TIMELINE")
        self.audit = audit


def timeline_audit(result, duration, *, window=None):
    lower, upper = window if window is not None else (0, duration)
    records = []
    for candidate in result.clips:
        segments = candidate.source_segments
        invalid = [s.model_dump() for s in segments if not (
            math.isfinite(s.start_seconds) and math.isfinite(s.end_seconds)
            and lower <= s.start_seconds < s.end_seconds <= upper <= duration)]
        payoff = candidate.payoff_timestamp
        bad_payoff = payoff is not None and not (
            math.isfinite(payoff) and lower <= payoff <= upper)
        reasons = []
        if window is not None:
            # These fields are source coordinates; subtitles may already be in output time.
            evidence = candidate.model_dump(include={
                "removed_regions", "visual_events", "required_context", "preserve_pauses",
                "reaction_moments", "cut_suggestions", "context_overlay", "micro_beats",
                "peak_moment", "reaction_window", "importance_curve", "hook_variants",
                "effect_events", "protected_regions"})
            def bad_time(value):
                if isinstance(value, list):
                    return any(bad_time(item) for item in value)
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in {"start_seconds", "end_seconds", "source_time", "time", "peak_moment"} and item is not None:
                            if not math.isfinite(item) or not lower <= item <= upper:
                                return True
                        elif bad_time(item):
                            return True
                return False
            if bad_time(evidence):
                reasons.append("EVENT_OUT_OF_WINDOW")
        if not segments:
            reasons.append("EMPTY_SOURCE_SEGMENTS")
        if invalid:
            reasons.append("SOURCE_SEGMENT_OUT_OF_BOUNDS")
        if bad_payoff:
            reasons.append("PAYOFF_OUT_OF_SOURCE")
        records.append({"rank": candidate.rank, "title": candidate.title,
                        "source_duration": duration, "valid": not reasons,
                        "reasons": reasons, "invalid_segments": invalid,
                        "payoff_timestamp": payoff})
    return records
