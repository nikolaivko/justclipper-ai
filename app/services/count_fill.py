"""Create honest, non-overlapping source excerpts when exact count is requested."""

from __future__ import annotations

from app.models.schemas import ClipCandidate, EDLSegment


def _free_intervals(chosen: list[ClipCandidate], duration: float) -> list[tuple[float, float]]:
    occupied = sorted((max(0.0, segment.start_seconds),
                       min(duration, segment.end_seconds))
                      for candidate in chosen for segment in candidate.source_segments)
    merged = []
    for start, end in occupied:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    free = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        free.append((cursor, duration))
    return free


def _windows(free: list[tuple[float, float]], length: float) -> list[tuple[float, float]]:
    windows = []
    for start, end in free:
        count = int((end - start + 1e-6) // length)
        for index in range(count):
            begin = start + index * length
            windows.append((begin, min(end, begin + length)))
    return windows


def _speech_overlap(window: tuple[float, float], speech: list[dict]) -> float:
    start, end = window
    return sum(max(0.0, min(end, item.get("end", 0)) -
                   max(start, item.get("start", 0))) for item in speech)


def fill_requested_count(chosen: list[ClipCandidate], count: int,
                         duration: float, min_seconds: int, max_seconds: int,
                         analysis_data: dict | None = None) -> list[ClipCandidate]:
    """Keep verified picks, then distribute clearly labelled excerpts across unused time."""
    result = list(chosen[:count])
    needed = count - len(result)
    if needed <= 0:
        return result
    free = _free_intervals(result, duration)
    preferred = min(max_seconds, max(min_seconds, 20))
    pool = _windows(free, preferred)
    if len(pool) < needed:
        pool = _windows(free, min_seconds)
    if len(pool) < needed:
        return result

    speech = (analysis_data or {}).get("speech") or []
    selected = []
    for index in range(needed):
        start = index * len(pool) // needed
        end = (index + 1) * len(pool) // needed
        group = pool[start:end]
        selected.append(max(group, key=lambda window: _speech_overlap(window, speech)))
    for start, end in sorted(selected):
        label = f"{int(start // 60):02d}:{int(start % 60):02d}–{int(end // 60):02d}:{int(end % 60):02d}"
        result.append(ClipCandidate(
            rank=len(result) + 1,
            title=f"Source excerpt {label}",
            short_description="Timed excerpt from the original video. Review its content before rendering.",
            topic="Source excerpt",
            content_type="SOURCE_EXCERPT",
            reason="Added to reach the requested clip count; editorial value has not been verified.",
            categories=["TIMELINE_EXCERPT"],
            context_dependency="UNKNOWN",
            source_segments=[EDLSegment(start_seconds=round(start, 3),
                                       end_seconds=round(end, 3),
                                       reason="Unreviewed source interval")],
            overall_score=0,
            editorial_score=0,
            decision_trace=["COUNT_FILL: review source excerpt before use"],
            selection_origin="TIMELINE_FILL",
        ))
    for rank, candidate in enumerate(result, 1):
        candidate.rank = rank
    return result
