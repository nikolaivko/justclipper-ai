"""Phrase emphasis and safe caption placement on the final selected timeline."""
import math
import re

from app.services.timeline import remap_intervals


class CaptionDirector:
    @staticmethod
    def plan(candidate, captions: list[dict], shots: list[dict], tracks: list[dict]):
        emphasis = {re.sub(r"[^\w]", "", word).casefold() for word in candidate.caption_emphasis[:3]}
        planned, animated = [], 0
        for original in captions:
            item = dict(original)
            start, end = item.get("start_seconds"), item.get("end_seconds")
            if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (start, end)) or end < start:
                raise ValueError("INVALID_CAPTION_TIME")
            if end == start:
                # Whisper can emit instantaneous punctuation/filler. It has no
                # display interval; preserve the other timed phrases unchanged.
                continue
            if not item.get("text", "").strip():
                continue
            item["emphasis"] = [word for word in item["text"].split()
                                if re.sub(r"[^\w]", "", word).casefold() in emphasis][:2]
            item["motion"] = "WORD_EMPHASIS" if item["emphasis"] else "APPEAR"
            if (item["emphasis"] and candidate.caption_motion == "SOFT_SCALE" and
                    candidate.style_profile not in {"EMOTIONAL_MINIMAL", "COMEDY_TIMING"} and animated < 2):
                item["motion"] = "SOFT_SCALE"
                animated += 1
            planned.append(item)
        regions = [r.model_dump() for r in candidate.protected_regions]
        positions = []
        for shot in shots:
            protected = [(r["y"], min(1, r["y"] + r["height"])) for r in regions
                         if r["start_seconds"] < shot["end"] and r["end_seconds"] > shot["start"]]
            protected.extend((face.get("y", 0), min(1, face.get("y", 0) + face.get("h", 0)))
                             for frame in tracks if shot["start"] <= frame.get("time", -1) <= shot["end"]
                             for face in frame.get("all_faces", []))
            overlay = candidate.context_overlay
            if overlay and overlay.start_seconds < shot["end"] and overlay.end_seconds > shot["start"]:
                protected.append((.12, .255))
            # Conservative full-width avoidance; no unsupported precision about crop x.
            def overlap(y):
                return sum(max(0, min(y + .135, high) - max(y, low)) for low, high in protected)
            position = min((.75, .57, .12, .35), key=overlap)
            shot["caption_y"] = position
            positions.append({"start": shot["output_start"], "end": shot["output_end"],
                              "y": position, "reason": "AVOID_PROTECTED_VISUALS" if protected else "LOWER_SAFE_AREA",
                              "overlap_risk": overlap(position) > 0})
        final_captions = remap_intervals(shots, planned)
        duration = shots[-1]["output_end"] if shots else 0
        if any(not 0 <= c["start_seconds"] < c["end_seconds"] <= duration + .001 for c in final_captions):
            raise ValueError("CAPTION_OUTSIDE_EDL")
        return planned, {"status": "READY" if planned else "NO_SPEECH_DETECTED",
                          "unit_count": len(final_captions), "positions": positions,
                          "emphasis_words": candidate.caption_emphasis[:3]}
