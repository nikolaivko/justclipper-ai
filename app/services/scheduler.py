import threading
import logging
from app.config import config

logger = logging.getLogger(__name__)

class TrendScheduler:
    def __init__(self):
        self.running = False
        self.thread = None
        self.stop_event = threading.Event()
        
    def start(self):
        if config.AUTONOMOUS_TRENDS_ENABLED and not self.running:
            self.stop_event.clear()
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            logger.info("Autonomous Trend Scheduler started.")
            
    def stop(self):
        self.running = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5.0)
            
    def _run_once(self):
        from app.services.youtube_service import youtube_service
        from app.services.download_service import download_service
        from app.services.job_store import job_store
        from app.services.heavy_gate import ProcessingBusy
        from app.services.storage_lifecycle import InsufficientStorage
        from app.services.source_rights_service import SourceRightsService

        if not config.YOUTUBE_DATA_API_KEY:
            return
        trends = youtube_service.get_us_trends(force_refresh=True)
        if not config.AUTO_PROCESS_ELIGIBLE_SOURCES:
            return

        for trend in trends:
            if not SourceRightsService.allows_auto_acquisition(trend.get("rights_status")):
                continue
            if trend.get("privacy_status") != "public" or trend.get("upload_status") != "processed":
                continue
            video_id = trend["id"]
            if job_store.get(f"job_youtube_{video_id}"):
                continue
            try:
                verified = youtube_service.get_video_rights(video_id)
            except Exception:
                logger.warning("Could not verify current rights for scheduled video %s", video_id)
                continue
            if (not verified or not SourceRightsService.allows_auto_acquisition(verified.get("rights_status"))
                    or verified.get("privacy_status") != "public"
                    or verified.get("upload_status") != "processed"):
                continue
            try:
                dl_id = download_service.enqueue_download(
                    video_id, verified.get("title") or trend["title"],
                    rights_status=verified["rights_status"],
                    rights_evidence=verified.get("rights_evidence", ""),
                    source_video_title=verified.get("title") or "",
                    source_channel_name=verified.get("channel", ""),
                    source_channel_id=verified.get("channel_id", ""),
                    public_verified=True,
                )
            except ValueError:
                logger.warning("Skipping invalid trend video ID")
                continue
            except (ProcessingBusy, InsufficientStorage):
                logger.info("Autonomous acquisition deferred: processing busy or disk guard")
                break
            status = download_service.get_status(dl_id).get("status")
            if status in ("QUEUED", "DOWNLOADING"):
                break
            elif status in ("ACQUISITION_BLOCKED", "AUTH_REQUIRED",
                            "RIGHTS_BLOCKED", "UNAVAILABLE", "FAILED"):
                logger.info("Skipping unavailable trend %s: %s", video_id, status)

    def _loop(self):
        if self.stop_event.wait(10):
            return
        while self.running:
            try:
                self._run_once()
            except Exception:
                logger.exception("Trend scheduler scan failed")
            self.stop_event.wait(config.TREND_SCAN_INTERVAL_MINUTES * 60)

scheduler = TrendScheduler()
