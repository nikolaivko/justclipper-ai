"""Canonical mapping from ordered source shots to the final output clock."""


def with_output_times(shots: list[dict]) -> list[dict]:
    result = []
    cursor = 0.0
    for shot in shots:
        duration = shot["end"] - shot["start"]
        if duration <= 0:
            continue
        result.append({**shot, "output_start": cursor,
                       "output_end": cursor + duration})
        cursor += duration
    return result


def remap_intervals(shots: list[dict], events: list[dict],
                    start_key: str = "start_seconds",
                    end_key: str = "end_seconds") -> list[dict]:
    """Split source-time events at every cut, retaining any extra event fields."""
    mapped = []
    for shot in with_output_times(shots):
        for event in events:
            begin = max(shot["start"], event[start_key])
            end = min(shot["end"], event[end_key])
            if end > begin:
                mapped.append({**event,
                               start_key: shot["output_start"] + begin - shot["start"],
                               end_key: shot["output_start"] + end - shot["start"]})
    return mapped
