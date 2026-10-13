"""Build truthful per-render YouTube metadata from the existing edit plan."""

import re

from app.models.schemas import YouTubeMetadata
from app.services.source_store import source_store
from app.services.youtube_source import source_attribution


GENERIC_TITLES = {"clip", "highlights", "clip highlights", "generated clip"}
CATEGORY_HASHTAGS = {
    "FUNNY": "FunnyMoments", "JOKE": "Comedy", "BANTER": "Banter",
    "REACTION": "Reaction", "WHOLESOME": "Wholesome", "EMOTIONAL": "EmotionalMoment",
    "FAIL": "FunnyFail", "DEMONSTRATION": "Demonstration", "ACTION": "Action",
    "STORY": "StoryTime", "SURPRISING": "SurprisingMoment",
}
STOP_WORDS = {"a", "an", "and", "at", "by", "for", "from", "in", "is", "it",
              "of", "on", "the", "this", "to", "was", "with", "video", "clip",
              "short", "shorts", "highlight", "highlights"}


INTERNAL_LABELS = {"source excerpt", "timeline excerpt", "unknown", "video", "general"}


def is_placeholder_title(value: object) -> bool:
    title = " ".join(str(value or "").replace("_", " ").split()).casefold()
    return (not title or title in GENERIC_TITLES | {"short video", "untitled"} or
            bool(re.match(r"^(source excerpt|timeline excerpt)(?:\s|$)", title)))


def _public_topic(value: object) -> bool:
    return bool(value) and str(value).replace("_", " ").casefold() not in INTERNAL_LABELS


def _clean(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def _hashtag(value: object) -> str:
    words = re.findall(r"\w+", str(value or ""), flags=re.UNICODE)
    return "".join(word.title() if word.isupper() else word[:1].upper() + word[1:]
                   for word in words)[:50]


def _dedupe(values: list[str], limit: int) -> list[str]:
    result, seen = [], set()
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
        if len(result) >= limit:
            break
    return result


def build_youtube_metadata(job: dict, render: dict, candidate: dict | None = None) -> dict:
    """Build metadata from a grounded candidate and preserve source attribution."""
    candidate = candidate or next((item for item in job.get("candidates", [])
                                   if item.get("rank") == render.get("rank")), {})
    story = candidate.get("story_arc") or {}
    options = (candidate.get("title"), render.get("title"), story.get("hook"),
               candidate.get("topic"), candidate.get("short_description"))
    title = next((_clean(value, 100) for value in options
                  if _clean(value, 100).casefold() not in GENERIC_TITLES
                  and _clean(value, 100)), "Short video")
    attribution = source_attribution(job)
    if attribution and job.get("source_id"):
        asset = source_store.get(job["source_id"])
        if asset:
            asset_data = source_attribution({**asset.model_dump(), "source_id": job["source_id"]})
            for key in ("source_video_title", "source_channel_name", "source_channel_id", "rights_evidence"):
                attribution[key] = attribution.get(key) or asset_data.get(key)
    source_title = _clean(attribution.get("source_video_title"), 180)
    source_channel = _clean(attribution.get("source_channel_name"), 120)
    source_url = attribution.get("source_url")

    summary = _clean(candidate.get("short_description") or story.get("setup") or
                     story.get("payoff") or title, 600)
    if summary.startswith("Timed excerpt from the original video."):
        summary = ""
    if summary and summary[-1] not in ".!?":
        summary += "."

    topics = [candidate.get("topic"), candidate.get("content_type"),
              *(CATEGORY_HASHTAGS.get(value, value) for value in
                (candidate.get("categories") or []))]
    phrases = ["Shorts", "YouTubeShorts"]
    phrases.extend(_hashtag(value) for value in topics if _public_topic(value))
    phrases.extend(_hashtag(word) for word in re.findall(r"\w+", title)
                   if not is_placeholder_title(title) and len(word) > 2 and word.casefold() not in STOP_WORDS)
    if source_channel:
        phrases.append(_hashtag(source_channel))
    hashtags = [f"#{value}" for value in _dedupe(phrases, 15)]

    tag_candidates = [candidate.get("topic"), candidate.get("content_type"),
                      *(candidate.get("categories") or []), title, source_channel]
    tags = _dedupe([_clean(value, 80) for value in tag_candidates
                   if _public_topic(value) and not is_placeholder_title(value)], 15)
    bounded_tags, size = [], 0
    for tag in tags:
        if size + len(tag) + 1 > 450:
            break
        bounded_tags.append(tag)
        size += len(tag) + 1

    description_parts = [summary]
    if source_title:
        description_parts.append(f"🎥 Original video: {source_title}")
    if source_channel:
        description_parts.append(f"📺 Original creator/channel: {source_channel}")
    if source_url:
        description_parts.append(f"🔗 Source: {source_url}")
    description_parts.append("✂️ Edited with justClipper")
    if attribution:
        origin = (f"by {source_channel}" if source_channel else "on YouTube")
        description_parts.append(
            "⚠️ Copyright & Source Notice:\n"
            f"This video contains excerpts from content originally published {origin}. "
            "All rights to the original footage and other third-party material remain "
            "with their respective copyright owners.\n\n"
            "This edited version may include selection, reframing, captions, pacing, "
            "and other edits created with justClipper.")
    description_parts.append(" ".join(hashtags))
    description = "\n\n".join(part for part in description_parts if part)[:5000]
    language = "en" if all(ord(char) < 128 for char in title + summary) else None
    return YouTubeMetadata(title=title, description=description,
                           hashtags=hashtags, tags=bounded_tags,
                           source_title=source_title or None,
                           source_channel=source_channel or None,
                           source_video_id=attribution.get("source_video_id"),
                           source_video_title=source_title or None,
                           source_channel_name=source_channel or None,
                           source_channel_id=attribution.get("source_channel_id") or None,
                           source_url=source_url,
                           rights_status=attribution.get("rights_status"),
                           rights_evidence=attribution.get("rights_evidence") or None,
                           language=language).model_dump()
