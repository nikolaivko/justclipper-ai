"""Recover missing editorial titles by inspecting the actual rendered short."""

import logging
from pathlib import Path

from google.genai import types
from pydantic import BaseModel, Field

from app.services.gemini_service import GeminiService
from app.services.gemini_requests import GenerationSession, GenerationFailure
from app.config import config
from app.services.youtube_metadata import build_youtube_metadata, is_placeholder_title

logger = logging.getLogger(__name__)


class ClipDescription(BaseModel):
    title: str = Field(min_length=4, max_length=100)
    short_description: str = Field(min_length=10, max_length=600)
    topic: str = Field(min_length=2, max_length=60)


class TitleGenerationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def title_error_code(exc: Exception) -> str:
    if isinstance(exc, TitleGenerationError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "GEMINI_TIMEOUT"
    if isinstance(exc, GenerationFailure) and exc.__cause__:
        return title_error_code(exc.__cause__)
    return {400: "GEMINI_REQUEST_REJECTED", 401: "GEMINI_AUTH_REQUIRED",
            403: "GEMINI_PERMISSION_DENIED", 404: "GEMINI_MODEL_UNAVAILABLE",
            429: "GEMINI_QUOTA_EXCEEDED", 500: "GEMINI_UNAVAILABLE",
            503: "GEMINI_UNAVAILABLE"}.get(getattr(exc, "code", None), "TITLE_GENERATION_FAILED")


def describe_clip(path: Path) -> dict:
    service = GeminiService()
    video = service.upload_video(path)
    try:
        preferred = config.GEMINI_METADATA_MODEL or service.model_name
        models = config.gemini_models(preferred)[:3]
        prompt = (
            "Watch and listen to this entire short video. Create its YouTube title, "
            "a factual one-sentence description, and a specific topic. Use the language "
            "spoken in the clip (English if no speech). The title should be concise, "
            "usually 4-12 words, and identify this particular action, exchange or insight. "
            "Use only events actually visible or audible here. Do not invent names, "
            "quotes, results, motives or context from outside this clip. Avoid vague "
            "clickbait such as 'You won't believe', 'Amazing moment', 'Highlights'. "
            "Never output Source excerpt, Clip N, timestamps, hashtags, internal editing "
            "labels or instructions to review the video. Return only the JSON object."
        )
        last_code = "TITLE_GENERATION_FAILED"
        session = GenerationSession()
        for model in models:
            try:
                response = session.generate(service.client, [model],
                    contents=[video, prompt], phase="metadata",
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema={"type": "OBJECT", "properties": {
                            field: {"type": "STRING"} for field in
                            ("title", "short_description", "topic")},
                            "required": ["title", "short_description", "topic"]}))
                result = ClipDescription.model_validate_json(response.text).model_dump()
                result["title"] = " ".join(result["title"].split())
                if is_placeholder_title(result["title"]) or any(c in result["title"] for c in "<>"):
                    raise ValueError("PLACEHOLDER_TITLE")
                return result
            except Exception as exc:
                last_code = title_error_code(exc)
                logger.warning("Clip title generation failed: model=%s type=%s code=%s",
                               model, type(exc).__name__, last_code)
        raise TitleGenerationError(last_code)
    finally:
        service.delete_video(video.name)


def ensure_clip_metadata(job: dict, render: dict, path: Path,
                         candidate: dict | None = None, *, force: bool = False) -> None:
    metadata = render.get("youtube_metadata") or build_youtube_metadata(job, render, candidate)
    if is_placeholder_title(metadata.get("title")):
        # Also clean legacy metadata created before the hashtag filter existed.
        metadata = build_youtube_metadata(job, render, candidate)
    render["youtube_metadata"] = metadata
    if not force and not is_placeholder_title(metadata.get("title")):
        return
    try:
        details = describe_clip(path)
        # Do not carry source-excerpt categories or fabricated story hooks forward.
        grounded = {"rank": render["rank"], **details}
        render["youtube_metadata"] = build_youtube_metadata(job, render, grounded)
        render["title"] = details["title"]
        render["generated_description"] = details["short_description"]
        render["generated_topic"] = details["topic"]
        render["metadata_status"] = "GENERATED"
        render.pop("metadata_error", None)
    except Exception as exc:
        logger.warning("Title unavailable for job=%s rank=%s type=%s",
                       job.get("id"), render.get("rank"), type(exc).__name__)
        if is_placeholder_title(metadata.get("title")):
            render["metadata_status"] = "REVIEW_REQUIRED"
        render["metadata_error"] = title_error_code(exc)
