import json
import logging
import os
import subprocess
import time
from pathlib import Path
from app.config import config
from app.services.json_storage import atomic_write_json, record_lock
from typing import Dict, Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)


def classify_upload_error(exc: Exception) -> str:
    reasons = set()
    content = getattr(exc, "content", None)
    if content:
        try:
            payload = json.loads(content)
            reasons = {item.get("reason", "") for item in
                       payload.get("error", {}).get("errors", [])}
        except (ValueError, TypeError, AttributeError):
            pass
    if reasons.intersection({"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}):
        return "API_QUOTA_EXCEEDED"
    if "uploadLimitExceeded" in reasons:
        return "CHANNEL_UPLOAD_LIMIT"
    if reasons.intersection({"userRateLimitExceeded", "rateLimitExceeded"}):
        return "RATE_LIMITED"
    if reasons.intersection({"authError", "invalidCredentials", "unauthorized"}):
        return "REAUTH_REQUIRED"
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 401:
        return "REAUTH_REQUIRED"
    message = str(exc).lower()
    if "upload limit" in message:
        return "CHANNEL_UPLOAD_LIMIT"
    if "rate limit" in message:
        return "RATE_LIMITED"
    if "quota" in message or "daily limit" in message:
        return "QUOTA_EXCEEDED"
    if any(term in message for term in ("auth", "credentials", "unauthorized")):
        return "REAUTH_REQUIRED"
    return "UPLOAD_FAILED"

class YouTubePublisher:
    def __init__(self):
        self.data_dir = config.DATA_ROOT / "youtube"
        self.token_file = self.data_dir / "token.json"
        
    def _get_credentials(self) -> Optional[Credentials]:
        if not self.token_file.exists():
            return None
            
        try:
            with open(self.token_file, "r") as f:
                creds_data = json.load(f)
            creds = Credentials.from_authorized_user_info(creds_data)
            
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(GoogleAuthRequest())
                    creds_data["token"] = creds.token
                    if creds.expiry:
                        creds_data["expiry"] = creds.expiry.isoformat().removesuffix("+00:00") + "Z"
                    with record_lock(self.token_file):
                        current = json.loads(self.token_file.read_text()) if self.token_file.exists() else {}
                        if current.get("refresh_token") != creds_data.get("refresh_token"):
                            return None
                        atomic_write_json(self.token_file, creds_data, durable=True)
                else:
                    return None
            return creds
        except Exception:
            logger.warning("Failed to load or refresh YouTube credentials")
            return None

    def validate_file(self, file_path: Path) -> bool:
        if not file_path.is_file() or file_path.suffix.lower() != ".mp4":
            return False
        
        if file_path.stat().st_size == 0:
            return False
            
        try:
            result = subprocess.run([
                config.get_ffprobe_path(), '-v', 'error',
                '-show_entries', 'format=duration:stream=codec_type',
                '-of', 'json',
                str(file_path)
            ], capture_output=True, text=True, check=True, timeout=20)
            data = json.loads(result.stdout)

            format_info = data.get("format", {})
            duration = float(format_info.get("duration", 0))
            if duration <= 0:
                return False

            streams = data.get("streams", [])
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)

            if not (has_video and has_audio):
                return False

        except Exception:
            return False

        return True

    def publish_video(
        self, 
        file_path: Path, 
        title: str, 
        description: str, 
        tags: list[str] = None,
        privacy_status: str | None = None,
        made_for_kids: bool = False
    ) -> Dict[str, Any]:
        """
        Uploads a video to YouTube using the official API.
        """
        creds = self._get_credentials()
        if not creds:
            return {"status": "REAUTH_REQUIRED", "error": "No valid credentials"}
            
        if not self.validate_file(file_path):
            return {"status": "FAILED", "error": "Invalid video file (missing or empty or corrupted)"}
            
        try:
            import httplib2
            import google_auth_httplib2
            http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
            youtube = build("youtube", "v3", http=http)
            bounded_tags = []
            tag_length = 0
            for tag in tags or []:
                tag = str(tag).strip()
                cost = len(tag) + (2 if " " in tag else 0) + (1 if bounded_tags else 0)
                if not tag or tag_length + cost > 500:
                    continue
                bounded_tags.append(tag)
                tag_length += cost
            
            body = {
                "snippet": {
                    "title": title[:100],  # YouTube max length
                    "description": description[:5000], # YouTube max length
                    "tags": bounded_tags,
                    "categoryId": "22"  # People & Blogs
                },
                "status": {
                    "privacyStatus": privacy_status or config.YOUTUBE_DEFAULT_PRIVACY,
                    "selfDeclaredMadeForKids": made_for_kids
                }
            }
            
            # Use resumable upload
            media = MediaFileUpload(
                str(file_path),
                mimetype="video/mp4",
                resumable=True
            )
            
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media
            )
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"Uploaded {int(status.progress() * 100)}%")
                    
            return {
                "status": "UPLOADED",
                "video_id": response["id"],
                "published_at": time.time(),
                "privacy_status": response.get("status", {}).get("privacyStatus"),
            }
            
        except Exception as e:
            code = classify_upload_error(e)
            logger.warning("YouTube upload failed: %s", code)
            return {"status": "REAUTH_REQUIRED" if code == "REAUTH_REQUIRED" else "FAILED",
                    "error": code}

youtube_publisher = YouTubePublisher()
