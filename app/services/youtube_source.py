"""Canonical, lightweight attribution for a validated YouTube source."""

import re
from urllib.parse import urlsplit, parse_qs


VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def parse_youtube_url(value: str) -> str:
    """Only video URLs on exact YouTube hosts; yt-dlp never receives raw input."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("INVALID_YOUTUBE_URL")
    try:
        url = urlsplit(value.strip())
        if url.scheme not in {"https", "http"} or url.username or url.password or url.port:
            raise ValueError()
        if url.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"} and url.path == "/watch":
            values = parse_qs(url.query).get("v", [])
            video_id = values[0] if len(values) == 1 else ""
        elif url.hostname in {"youtu.be", "www.youtu.be"}:
            video_id = url.path.removeprefix("/")
        else:
            video_id = ""
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError()
        return video_id
    except ValueError:
        raise ValueError("INVALID_YOUTUBE_URL") from None


def canonical_watch_url(video_id: str) -> str | None:
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def source_attribution(record: dict) -> dict:
    """Return only known fields; never trust a stored or submitted source URL."""
    source_id = record.get("source_id") or ""
    if not source_id.startswith("youtube_"):
        return {}
    video_id = source_id.removeprefix("youtube_")
    url = canonical_watch_url(video_id)
    if not url or (record.get("source_video_id") and record["source_video_id"] != video_id):
        return {}
    return {
        "source_video_id": video_id,
        "source_video_title": record.get("source_video_title") or "",
        "source_channel_name": record.get("source_channel_name") or record.get("source_channel") or "",
        "source_channel_id": record.get("source_channel_id") or "",
        "source_url": url,
        "rights_status": record.get("rights_status") or "UNKNOWN",
        "rights_evidence": record.get("rights_evidence") or record.get("rights_reason") or "",
    }
