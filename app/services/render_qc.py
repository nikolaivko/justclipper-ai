"""Cheap metadata and sampled-frame checks after final render."""

from pathlib import Path

from app.models.schemas import VideoMetadata


def check_render(path: Path, meta: VideoMetadata, shots: list[dict],
                 expect_motion: bool = False) -> str | None:
    if (meta.width, meta.height) != (1080, 1920) and \
            (meta.width, meta.height) != (720, 1280):
        return "INVALID_OUTPUT_SIZE"
    if not meta.audio_codec or not meta.video_codec:
        return "MISSING_AUDIO_OR_VIDEO"
    expected = shots[-1]["output_end"] if shots else 0
    if expected <= 0 or abs(meta.duration - expected) > max(.35, expected * .02):
        return "OUTPUT_DURATION_MISMATCH"
    try:
        import cv2
        import numpy as np
        capture = cv2.VideoCapture(str(path))
        samples = []
        for second in (.05, meta.duration * .27, meta.duration * .51,
                       meta.duration * .75, max(.05, meta.duration - .1)):
            capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            gray = cv2.cvtColor(cv2.resize(frame, (96, 160)), cv2.COLOR_BGR2GRAY)
            samples.append(gray)
        capture.release()
        if len(samples) < 3:
            return "UNREADABLE_OUTPUT_FRAMES"
        if sum(float(np.mean(frame)) < 4 for frame in samples) >= 3:
            return "BLACK_OUTPUT_FRAMES"
        if float(np.mean(samples[0])) < 4 or float(np.mean(samples[-1])) < 4:
            return "BLACK_OPENING_OR_ENDING"
        if expect_motion and meta.duration >= 4 and all(
                float(np.mean(cv2.absdiff(a, b))) < .3
                for a, b in zip(samples, samples[1:])):
            return "UNEXPECTED_FROZEN_OUTPUT"
    except (ImportError, OSError, ValueError):
        # ffprobe remains the mandatory gate when sampled decoding is absent.
        return None
    return None
