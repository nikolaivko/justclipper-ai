"""Validate a proposed edit without mutating its original semantic evidence."""
import math
from app.services.editorial_service import validate_final_edl


def validate_compression(candidate, proposed, settings):
    if not proposed:
        return 'EMPTY_COMPRESSION_EDL'
    original = candidate.semantic_source_segments or candidate.source_segments
    # Merge adjacent/overlapping allowed intervals; never bridge a semantic gap.
    allowed = []
    for segment in sorted(original, key=lambda s: s.start_seconds):
        start, end = segment.start_seconds, segment.end_seconds
        if allowed and start <= allowed[-1][1]:
            allowed[-1][1] = max(allowed[-1][1], end)
        else:
            allowed.append([start, end])
    previous = -1
    for segment in proposed:
        start, end = segment.start_seconds, segment.end_seconds
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            return 'INVALID_COMPRESSION_TIMELINE'
        if start < previous:
            return 'INVALID_COMPRESSION_CHRONOLOGY'
        if not any(left <= start < end <= right for left, right in allowed):
            return 'COMPRESSION_OUTSIDE_SEMANTIC_RANGES'
        previous = end
    length = sum(s.end_seconds - s.start_seconds for s in proposed)
    if not max(5, settings.get('min_clip_seconds', 5)) <= length <= min(60, settings.get('max_clip_seconds', 60)):
        return 'UNUSABLE_COMPRESSION_DURATION'
    trial = candidate.model_copy(deep=True)
    trial.source_segments = proposed
    return validate_final_edl(trial, settings.get('source_duration') or max(end for _, end in allowed))
