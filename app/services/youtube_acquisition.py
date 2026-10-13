"""Bounded public-video acquisition with an explicitly imported local session."""

import importlib.util
import importlib
import json
import logging
import urllib.request
import urllib.error
import concurrent.futures
import time
import random
from pathlib import Path

import yt_dlp


logger = logging.getLogger(__name__)

RETRYABLE_ACQUISITION_ERRORS = {
    "BOT_PROTECTION",
    "PO_TOKEN_PROVIDER_UNAVAILABLE",
    "HTTP_403",
    "HTTP_429",
    "NETWORK_ERROR",
    "UNKNOWN", "FORMAT_UNAVAILABLE", "INTERRUPTED", "SESSION_EXPIRED"
}


def local_youtube_session():
    root = Path(__file__).resolve().parents[2]
    path = root/'data/config/youtube-cookies.txt'
    return path if path.is_file() and path.resolve().is_relative_to(root) else None


class AcquisitionFailure(Exception):
    def __init__(self, status: str, code: str, attempts: list[dict] | None = None):
        self.status = status
        self.code = code
        self.attempts = attempts or []
        super().__init__(code)


def classify_error(message: str) -> tuple[str, str, bool]:
    lower = message.lower()
    if "youtube account cookies are no longer valid" in lower:
        return "AUTH_REQUIRED", "SESSION_EXPIRED", False
    if "no space left on device" in lower or "errno 28" in lower or "insufficient_storage" in lower:
        return "FAILED", "DISK_FULL", False
    if any(term in lower for term in ("copyright", "drm", "not available in your country", "geo-restricted")):
        return "RIGHTS_BLOCKED", "RIGHTS_BLOCKED", False
    if any(term in lower for term in ("private video", "login required", "requires login",
                                      "age-restricted", "age restricted", "confirm your age",
                                      "age verification", "members-only")):
        return "AUTH_REQUIRED", "AUTH_REQUIRED", False
    if any(term in lower for term in ("video unavailable", "video is unavailable",
                                      "has been removed", "does not exist")):
        return "UNAVAILABLE", "VIDEO_UNAVAILABLE", False
    if any(term in lower for term in ("error reaching get /ping", "error reaching post /get_pot",
                                      "server is not available", "no po token providers",
                                      "error parsing ping response", "error parsing response json",
                                      "failed to generate po token", "failed to extract initial attestation")):
        return "ACQUISITION_BLOCKED", "PO_TOKEN_PROVIDER_UNAVAILABLE", True
    if "sign in to confirm" in lower or "not a bot" in lower or "bot protection" in lower:
        return "ACQUISITION_BLOCKED", "BOT_PROTECTION", True
    if "http error 403" in lower or "http 403" in lower:
        return "ACQUISITION_BLOCKED", "HTTP_403", True
    if "http error 429" in lower or "http 429" in lower:
        return "ACQUISITION_BLOCKED", "HTTP_429", True
    if "http error 400" in lower or "http 400" in lower:
        return "FAILED", "HTTP_400", False
    if "requested format is not available" in lower or "no video formats" in lower:
        return "FAILED", "FORMAT_UNAVAILABLE", True
    if any(term in lower for term in ("timed out", "connection reset", "network error", "unable to download webpage")):
        return "FAILED", "NETWORK_ERROR", False
    return "FAILED", "UNKNOWN", False


class SafeYtdlpLogger:
    """Capture categories only; yt-dlp warnings may contain URLs or tokens."""
    def __init__(self):
        self.categories = []

    def debug(self, _message):
        pass

    def warning(self, message):
        self.categories.append(classify_error(str(message)))

    def error(self, message):
        self.categories.append(classify_error(str(message)))


def po_provider_ready(timeout: float = 2.0) -> bool:
    try:
        if importlib.util.find_spec("yt_dlp_plugins.extractor.getpot_bgutil_http") is None:
            return False
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=timeout) as response:
            payload = json.load(response)
            return response.status == 200 and isinstance(payload, dict) and bool(payload.get("version"))
    except Exception:
        return False


def acquire_public_video(video_id: str, work_dir: Path, cache_dir: Path,
                         validate_file, set_strategy, on_progress=None, *, download_limits=None) -> tuple[Path, dict, str]:
    """Return one validated MP4; try PO once only for relevant extractor failures."""
    final_status, final_code = "FAILED", "UNKNOWN"
    attempts = []
    strategies = [
        {"name": "STANDARD_YTDLP", "use_po": False, "client": "default"},
        {"name": "PO_TOKEN", "use_po": True, "client": "mweb"}
    ]
    
    for index, strat_config in enumerate(strategies, start=1):
        strategy = strat_config["name"]
        use_po = strat_config["use_po"]
        client = strat_config["client"]
        
        if use_po and not po_provider_ready():
            logger.warning("PO token provider unavailable for video %s", video_id)
            if index == len(strategies):
                final_status, final_code = "ACQUISITION_BLOCKED", "PO_TOKEN_PROVIDER_UNAVAILABLE"
            continue
        
        attempt_dir = work_dir / str(index)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        set_strategy(strategy)
        diagnostic_logger = SafeYtdlpLogger()
        options = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4][acodec!=none]",
            "merge_output_format": "mp4",
            "outtmpl": str(attempt_dir / "source.%(ext)s"),
            "cachedir": str(cache_dir),
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 0,
            "quiet": True,
            "logger": diagnostic_logger,
        }
        
        if download_limits:
            max_seconds, max_bytes = download_limits
            def limit_filter(info, *, incomplete=False):
                if info.get('duration', 0) and info['duration'] > max_seconds:
                    return 'Video exceeds the source duration limit'
                if info.get('filesize', 0) and info['filesize'] > max_bytes:
                    return 'Video exceeds the source size limit'
            options['match_filter'] = limit_filter
            options['max_filesize'] = max_bytes
            options['format'] = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4][acodec!=none]'
        session = local_youtube_session()
        if session:
            private_copy = attempt_dir/'youtube-cookies.txt'
            private_copy.touch(mode=0o600)
            private_copy.write_bytes(session.read_bytes())
            options['cookiefile'] = str(private_copy)
        logger.info('YouTube acquisition: strategy=%s session=%s', strategy, bool(session))
        if on_progress:
            options["progress_hooks"] = [on_progress]
        
        project_root = Path(__file__).resolve().parents[2]
        options["remote_components"] = ["ejs:github"]
        options["js_runtimes"] = {"deno": {"path": str(project_root/"bin/deno")}}
        options["ffmpeg_location"] = str(project_root/"bin")

        extractor_args = {}
        if client != "default":
            extractor_args["youtube"] = {"player_client": [client]}
            
        if use_po:
            if "youtube" not in extractor_args:
                extractor_args["youtube"] = {"player_client": ["web"]}
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": ["http://127.0.0.1:4416"]}
            
        if extractor_args:
            options["extractor_args"] = extractor_args
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=True)
            candidate = attempt_dir / "source.mp4"
            if validate_file(candidate):
                return candidate, info or {}, strategy
            final_status, final_code = "FAILED", "INVALID_OUTPUT"
            attempts.append({"strategy": strategy, "code": final_code,
                             "yt_dlp_exit_status": None})
        except Exception as exc:
            final_status, final_code, retry_with_po = classify_error(str(exc))
            session_expired = any(item[1] == "SESSION_EXPIRED"
                                  for item in diagnostic_logger.categories)
            if session_expired:
                # yt-dlp warns about rotated cookies before raising a generic
                # bot-check error. Another client with the same cookies cannot
                # repair the session; keep the actionable cause.
                final_status, final_code, retry_with_po = "AUTH_REQUIRED", "SESSION_EXPIRED", False
            elif use_po:
                provider_errors = [item for item in diagnostic_logger.categories
                                   if item[1] == "PO_TOKEN_PROVIDER_UNAVAILABLE"]
                if provider_errors:
                    final_status, final_code, retry_with_po = provider_errors[-1]
            attempts.append({"strategy": strategy, "code": final_code,
                             "yt_dlp_exit_status": None})
            logger.warning("YouTube extraction failed: video=%s strategy=%s code=%s exception_type=%s exit_status=NA",
                           video_id, strategy, final_code, type(exc).__name__)
            if use_po or not retry_with_po:
                break
    raise AcquisitionFailure(final_status, final_code, attempts)
