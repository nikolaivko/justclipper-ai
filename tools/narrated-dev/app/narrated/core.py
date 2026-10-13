"""Validated edit decisions and bounded, project-local media operations."""
import json
import math
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/narrated"
JOBS = DATA / "jobs"
VOICES = DATA / "voices"
MAX_SOURCE_SECONDS = 3 * 3600
MAX_SOURCE_BYTES = 8 * 1024**3


class Shot(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    # Gemini's Schema supports minimum, not JSON Schema exclusiveMinimum.
    # ordered() still enforces end >= start + 0.5, hence strictly positive.
    end: float = Field(ge=0, allow_inf_nan=False)
    evidence: str = Field(min_length=5, max_length=600)
    framing: Literal["portrait", "wide"] = "portrait"
    focus_x: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    framing_reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def ordered(self):
        if self.framing == "wide" and len(self.framing_reason.strip()) < 10:
            raise ValueError("Wide framing needs a specific visual reason")
        if not 0.5 <= self.end - self.start <= 20:
            raise ValueError("Each source shot must be 0.5–20 seconds")
        return self


class Scene(BaseModel):
    kind: Literal["narration", "original"]
    text: str = Field(min_length=1, max_length=400)
    shots: list[Shot] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def original_single_shot(self):
        if self.kind == "original" and len(self.shots) != 1:
            raise ValueError("Original dialogue must remain one continuous shot")
        if len(self.text.split()) > 24:
            raise ValueError("Keep each scene to at most 24 words for readable captions")
        return self


class Plan(BaseModel):
    title: str = Field(min_length=5, max_length=90)
    summary: str = Field(min_length=10, max_length=1000)
    language: Literal["en"]
    scenes: list[Scene] = Field(min_length=4, max_length=14)

    def check_source(self, duration: float):
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid source duration")
        for scene in self.scenes:
            for shot in scene.shots:
                if shot.end > duration + .02:
                    raise ValueError(f"Shot {shot.start}–{shot.end} seconds exceeds source duration {duration:.3f} seconds")
        words = sum(len(s.text.split()) for s in self.scenes if s.kind == "narration")
        if not 65 <= words <= 135:
            raise ValueError("English narration must contain 65–135 words")
        if sum(s.shots[0].end-s.shots[0].start for s in self.scenes if s.kind == "original") > 15:
            raise ValueError("Original dialogue exceeds 15 seconds")
        return self


def gemini_plan_schema():
    """Provider's portable schema subset; full constraints stay local."""
    schema = Plan.model_json_schema()
    definitions = schema.get('$defs', {})

    def convert(node):
        if '$ref' in node:
            return convert(definitions[node['$ref'].split('/')[-1]])
        result = {'type': node['type'].upper()}
        if 'properties' in node:
            result['properties'] = {key: convert(value) for key, value in node['properties'].items()}
        if 'items' in node:
            result['items'] = convert(node['items'])
        if 'required' in node:
            result['required'] = node['required']
        if 'enum' in node:
            result['enum'] = node['enum']
        if 'const' in node:
            result['enum'] = [node['const']]
        return result

    return convert(schema)


def run(args, *, timeout=600):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Media operation failed: {result.stderr[-1800:]}")
    return result.stdout


def probe(path):
    data = json.loads(run([ROOT/'bin/ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path], timeout=30))
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Empty or invalid media")
    return duration, data['streams']


def fit_shots(shots, speech_duration):
    """Keep footage at natural speed. Never freeze or repeat to fill narration."""
    available = sum(s.end - s.start for s in shots)
    if not math.isfinite(speech_duration) or speech_duration <= 0:
        raise ValueError("Generated speech is empty")
    if speech_duration > available + .02:
        raise ValueError("Narration is longer than its evidence footage; shorten this scene's text and retry")
    # Allocate proportionally so every planned visual remains represented.
    factor = speech_duration / available
    fitted = [(s.start, (s.end-s.start)*factor) for s in shots]
    if min(d for _, d in fitted) < .5:
        raise ValueError("Too many shots for this short sentence; simplify the scene")
    return fitted


def narration_durations(scenes, durations, *, allow_slowdown=True):
    """Fit the recap with at most 20% speech tempo correction, never freeze video."""
    lower, upper, chosen = [], [], []
    for scene, duration in zip(scenes, durations, strict=True):
        available = sum(s.end-s.start for s in scene.shots)
        if scene.kind == 'original':
            lo = hi = duration
        else:
            lo, hi = duration/1.2, min(duration/.8 if allow_slowdown else duration, available)
            if lo > hi:
                raise ValueError('A narration sentence is too long for its footage. Shorten that sentence or select more matching footage.')
        lower.append(lo); upper.append(hi); chosen.append(min(max(duration, lo), hi))
    total = sum(chosen)
    target = 40.5 if total < 40 and allow_slowdown else 58.5 if total > 59 else total
    if not sum(lower) <= target <= sum(upper):
        raise ValueError(f'The narration totals {sum(durations):.1f}s and cannot reach 40–59s at a natural pace with these shots. Add more narration and matching footage for a short script, or shorten a long script.')
    capacity = [(hi-d if target > total else d-lo) for d,lo,hi in zip(chosen,lower,upper)]
    if abs(target-total) > .001:
        ratio = (target-total)/sum(capacity)
        chosen = [d+ratio*c for d,c in zip(chosen,capacity)]
    return chosen


def write_json(path, data):
    temp = path.with_suffix('.new')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


def portrait_filter(shot, source_crop=None):
    """Stable shot-specific framing; always crop to fill vertical screen."""
    # Focus is a normalized position in the source, clamped to legal crop bounds.
    pre = f"[0:v]{source_crop}," if source_crop else "[0:v]"
    return (f"{pre}scale=1080:1920:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop=1080:1920:x='max(0,min(iw-ow,iw*{shot.focus_x:.6f}-ow/2))':y=(ih-oh)/2,"
            'setsar=1,fps=30[v]')


def youtube_video_id(url):
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url.strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in {'https', 'http'} or parsed.username or parsed.password:
        raise ValueError('Use a YouTube video link')
    if host == 'youtu.be':
        identifier = parsed.path.strip('/')
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
        parts = parsed.path.strip('/').split('/')
        identifier = (parse_qs(parsed.query).get('v', [''])[0] if parsed.path == '/watch'
                      else parts[1] if len(parts) == 2 and parts[0] in {'shorts', 'live', 'embed'} else '')
    else:
        raise ValueError('Use a YouTube video link')
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
        raise ValueError('Invalid YouTube video link')
    return identifier
