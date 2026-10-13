import os
import json
import time
from pathlib import Path
from googleapiclient.discovery import build
from app.config import config


from app.services.creator_metadata import calculate_creator_trend_score, classify_youtube_content

class YouTubeService:

    RIGHTS_CACHE_VERSION = 3

    def __init__(self, project_root: Path):
        self.api_key = getattr(config, "YOUTUBE_DATA_API_KEY", "") or ""
        self.project_root = project_root
        self.cache_dir = Path(project_root) / "cache" / "youtube"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "trends_cache.json"

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def _get_client(self):
        if not self.is_configured():
            raise ValueError("YouTube Data API Key is not configured")
        return build('youtube', 'v3', developerKey=self.api_key, cache_discovery=False)

    def get_video_rights(self, video_id: str) -> dict | None:
        """Check the current license for one public video before acquisition."""
        from app.services.source_rights_service import SourceRightsService
        response = self._get_client().videos().list(
            part="status,snippet", id=video_id, maxResults=1).execute()
        item = next((item for item in response.get("items", [])
                     if item.get("id") == video_id), None)
        if not item:
            return None
        snippet = item.get("snippet", {})
        rights = SourceRightsService.assess_rights(
            item, owned_channel_id=config.YOUTUBE_OWNED_CHANNEL_ID)
        status = item.get("status") or {}
        return {**rights,
                "rights_evidence": rights["rights_reason"],
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "privacy_status": status.get("privacyStatus", ""),
                "upload_status": status.get("uploadStatus", "")}

    def get_us_trends(self, max_results=50, force_refresh=False, page_token=None):
        from app.services.creator_trends import CreatorTrends
        return CreatorTrends(self).get(max_results, force_refresh, page_token)

youtube_service = YouTubeService(config.DATA_ROOT)
