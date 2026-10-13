from app.config import config
from concurrent.futures import ThreadPoolExecutor
import logging
import re
import shutil
import threading
import uuid
import time
import json
from pathlib import Path
from app.services.heavy_gate import heavy_gate
from app.services.storage_lifecycle import storage_lifecycle, InsufficientStorage
from app.services.source_rights_service import SourceRightsService
from app.services.youtube_source import canonical_watch_url

logger = logging.getLogger(__name__)
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")
TERMINAL_FAILURES = {"ACQUISITION_BLOCKED", "AUTH_REQUIRED", "RIGHTS_BLOCKED", "UNAVAILABLE", "FAILED"}

class DownloadService:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.cache_dir = project_root / ".cache" / "yt-dlp"
        self.temp_dir = project_root / "temp" / "downloads"
        self.trends_dir = project_root / "sources" / "trends" / "youtube"
        
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.trends_dir.mkdir(parents=True, exist_ok=True)
        
        self.active_downloads = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="youtube-acquisition")

    def stop(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            video_ids = {state["video_id"] for state in self.active_downloads.values()}
        for video_id in video_ids:
            heavy_gate.release(f"job_youtube_{video_id}")

    def _status_path(self, video_id: str) -> Path:
        return self.trends_dir / video_id / "acquisition.json"

    def _reusable_source(self, video_id: str) -> Path:
        """Registry media is reusable only inside this service's data root."""
        canonical = self.trends_dir / video_id / "source.mp4"
        if canonical.is_file():
            return canonical
        from app.services.source_store import source_store
        asset = source_store.get(f"youtube_{video_id}")
        if asset and asset.storage_status != "CLEANED" and asset.source_path:
            path = Path(asset.source_path).resolve()
            if path.is_relative_to(self.project_root.resolve()) and path.is_file():
                return path
        return canonical

    def acquisition_state(self, video_id: str) -> str:
        if not VIDEO_ID_RE.fullmatch(video_id):
            return "UNAVAILABLE"
        with self.lock:
            for state in self.active_downloads.values():
                if state["video_id"] == video_id and state["status"] in ("QUEUED", "DOWNLOADING"):
                    return state["status"]
        try:
            saved = json.loads(self._status_path(video_id).read_text())
        except (OSError, ValueError):
            source = self.trends_dir / video_id / "source.mp4"
            return "COMPLETED" if self._validate_with_ffprobe(source) else "UNVERIFIED"
        status = saved.get("status", "UNVERIFIED")
        if status == "COMPLETED" and not self._validate_with_ffprobe(
                self.trends_dir / video_id / "source.mp4"):
            from app.services.source_store import source_store
            asset = source_store.get(f"youtube_{video_id}")
            if asset and asset.storage_status == "CLEANED":
                return "COMPLETED"
            return "FAILED"
        return "FAILED" if status in ("QUEUED", "DOWNLOADING") else status

    def acquisition_diagnostic(self, video_id: str) -> str | None:
        if not VIDEO_ID_RE.fullmatch(video_id):
            return None
        try:
            saved = json.loads(self._status_path(video_id).read_text())
        except (OSError, ValueError):
            return None
        code = saved.get("error_code")
        return code if isinstance(code, str) and re.fullmatch(r"[A-Z_0-9]{1,64}", code) else None

    def technical_downloadability(self, video_id: str) -> str:
        """Observed transfer outcome, independent of reuse permission."""
        state = self.acquisition_state(video_id)
        if state == "COMPLETED":
            return "CONFIRMED"
        if state in ("ACQUISITION_BLOCKED", "AUTH_REQUIRED", "UNAVAILABLE"):
            return "BLOCKED"
        return "UNVERIFIED"

    def _set_status(self, dl_id: str, video_id: str, **changes):
        with self.lock:
            self.active_downloads[dl_id].update(changes)
            self.active_downloads[dl_id]["updated_at"] = time.time()
            snapshot = self.active_downloads[dl_id].copy()
        status_file = self._status_path(video_id)
        status_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = status_file.with_suffix(f".{dl_id}.tmp")
        temporary.write_text(json.dumps(snapshot))
        temporary.replace(status_file)

    def enqueue_download(self, video_id: str, title: str, *,
                         rights_status: str = "UNKNOWN", rights_evidence: str = "",
                         source_video_title: str | None = None,
                         source_channel_name: str = "", source_channel_id: str = "",
                         public_verified: bool = False, force_retry: bool = False,
                         auto_edit_settings: dict | None = None) -> str:
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("INVALID_VIDEO_ID")
        if not SourceRightsService.allows_public_acquisition(
                rights_status, public_verified=public_verified):
            raise ValueError("RIGHTS_BLOCKED" if rights_status == "RESTRICTED" else "RIGHTS_UNVERIFIED")
        with self.lock:
            for existing_id, info in self.active_downloads.items():
                if info["video_id"] == video_id and info["status"] in ("QUEUED", "DOWNLOADING"):
                    if auto_edit_settings is not None:
                        previous = info.get("auto_edit_settings")
                        if previous and previous != auto_edit_settings:
                            from app.services.heavy_gate import ProcessingBusy
                            raise ProcessingBusy("PROCESSING_BUSY")
                        info["auto_edit_settings"] = dict(auto_edit_settings)
                    return existing_id
                if info["video_id"] == video_id and info["status"] == "COMPLETED":
                    from app.services.job_store import job_store
                    existing_job = job_store.get(info.get("job_id") or "")
                    if existing_job and existing_job.get("status") in (
                        "QUEUED_ANALYSIS", "PREPARING_ANALYSIS_MEDIA", "GENERATING_PROXY",
                        "UPLOADING_TO_GEMINI", "WAITING_FOR_GEMINI", "VALIDATING_CANDIDATES",
                        "QUEUED_RENDER", "RENDERING"):
                        if auto_edit_settings is not None and any(
                                existing_job.get("analysis_settings", {}).get(key) != auto_edit_settings.get(key)
                                for key in ("auto_edit", "count_mode", "target_clip_count")):
                            from app.services.heavy_gate import ProcessingBusy
                            raise ProcessingBusy("PROCESSING_BUSY")
                        return existing_id
            from app.services.job_store import job_store
            from app.services.storage_lifecycle import ACTIVE_STATUSES
            running = job_store.get(f"job_youtube_{video_id}")
            if running and running.get("status") in ACTIVE_STATUSES:
                from app.services.heavy_gate import ProcessingBusy
                raise ProcessingBusy("PROCESSING_BUSY")
            # Removed PUBLISHED_JOB_ANALYSIS_LOCKED check per user request to never block manual re-import
            heavy_gate.claim(f"job_youtube_{video_id}")
            dl_id = str(uuid.uuid4())
            self.active_downloads[dl_id] = {
                "id": dl_id, "video_id": video_id, "title": title,
                "status": "QUEUED", "progress": 0.0, "error": None,
                "error_code": None, "job_id": None,
                "lifecycle_version": 1,
                "auto_edit_settings": auto_edit_settings,
            }

        try:
            storage_lifecycle.prepare_for_new_video()
        except Exception:
            logger.warning("Safe storage preflight cleanup could not complete")
        final_mp4 = self._reusable_source(video_id)
        from app.services.source_store import source_store
        from app.services.job_store import job_store
        asset = source_store.get(f"youtube_{video_id}")
        completed_job = job_store.get(f"job_youtube_{video_id}")
        reusable = completed_job and any(
            (render.get("storage_status") != "CLEANED" and render.get("output_path") and
             Path(render["output_path"]).is_file()) or
            (render.get("publish_status") == "UPLOADED" and render.get("youtube_video_id"))
            for render in completed_job.get("renders", []))
        if (auto_edit_settings is None and asset and asset.storage_status == "CLEANED" and completed_job
                and completed_job.get("status") == "COMPLETED" and reusable):
            self._set_status(dl_id, video_id, status="COMPLETED", progress=1.0,
                             job_id=completed_job["id"], strategy="EXISTING_JOB")
            heavy_gate.release(f"job_youtube_{video_id}")
            return dl_id
        try:
            if final_mp4.exists() and self._validate_with_ffprobe(final_mp4):
                storage_lifecycle.require_space()
            else:
                storage_lifecycle.require_acquisition_space()
        except Exception:
            with self.lock:
                self.active_downloads.pop(dl_id, None)
            heavy_gate.release(f"job_youtube_{video_id}")
            raise
        if final_mp4.exists() and self._validate_with_ffprobe(final_mp4):
            try:
                job_id = self._register_source_and_job(
                    video_id, title, rights_status, rights_evidence=rights_evidence,
                    source_video_title=source_video_title,
                    source_channel=source_channel_name, source_channel_id=source_channel_id,
                    auto_edit_settings=auto_edit_settings)
                self._set_status(dl_id, video_id, status="COMPLETED", progress=1.0,
                                 job_id=job_id, strategy="CACHED")
            except Exception:
                logger.exception("Failed to register cached source %s", video_id)
                self._set_status(dl_id, video_id, status="FAILED", error="REGISTRATION_FAILED",
                                 error_code="REGISTRATION_FAILED")
                heavy_gate.release(f"job_youtube_{video_id}")
            else:
                if job_store.get(job_id) and job_store.get(job_id).get("status") not in (
                    "QUEUED_ANALYSIS", "PREPARING_ANALYSIS_MEDIA", "GENERATING_PROXY",
                    "UPLOADING_TO_GEMINI", "WAITING_FOR_GEMINI", "VALIDATING_CANDIDATES",
                    "QUEUED_RENDER", "RENDERING"):
                    heavy_gate.release(f"job_youtube_{video_id}")
            return dl_id

        try:
            previous = json.loads(self._status_path(video_id).read_text())
        except (OSError, ValueError):
            previous = {}
        if not force_retry and previous.get("status") in TERMINAL_FAILURES:
            code = previous.get("error_code") or "PREVIOUS_FAILURE"
            self._set_status(dl_id, video_id, status=previous["status"],
                             error=code, error_code=code)
            heavy_gate.release(f"job_youtube_{video_id}")
            return dl_id

        self._set_status(dl_id, video_id, status="QUEUED")
        try:
            self.executor.submit(self._download_worker, dl_id, video_id, title, rights_status,
                                 rights_evidence, source_video_title,
                                 source_channel_name, source_channel_id)
        except Exception:
            heavy_gate.release(f"job_youtube_{video_id}")
            self._set_status(dl_id, video_id, status="FAILED", error="QUEUE_FAILED",
                             error_code="QUEUE_FAILED")
            raise
        return dl_id

    def get_status(self, dl_id: str) -> dict:
        with self.lock:
            current = self.active_downloads.get(dl_id)
            if current:
                return current.copy()
        # Polling can continue after a Railway restart; recover terminal state.
        for status_file in self.trends_dir.glob("*/acquisition.json"):
            try:
                saved = json.loads(status_file.read_text())
            except (OSError, ValueError):
                continue
            if saved.get("id") == dl_id:
                if saved.get("status") in ("QUEUED", "DOWNLOADING"):
                    saved.update(status="FAILED", error="INTERRUPTED", error_code="INTERRUPTED")
                return saved
        return {}

    def reconcile_interrupted(self):
        """At startup, retire new-version downloads interrupted by a restart."""
        for status_file in self.trends_dir.glob("*/acquisition.json"):
            try:
                saved = json.loads(status_file.read_text())
                dl_id = saved.get("id", "")
                if (saved.get("lifecycle_version") != 1 or
                        saved.get("status") not in ("QUEUED", "DOWNLOADING") or
                        not isinstance(dl_id, str) or not uuid.UUID(dl_id)):
                    continue
                saved.update(status="FAILED", error="INTERRUPTED", error_code="INTERRUPTED")
                temporary = status_file.with_suffix(".recovery.tmp")
                temporary.write_text(json.dumps(saved))
                temporary.replace(status_file)
                # The storage reconciler removes only recognized partial files
                # after its grace period; it never recursively deletes a work dir.
            except (OSError, ValueError, TypeError):
                logger.warning("Could not reconcile an interrupted download")



    def _register_source_and_job(self, video_id: str, title: str,
                                 rights_status: str = "UNKNOWN",
                                 new_acquisition: bool = False,
                                 source_channel: str | None = None,
                                 source_channel_id: str | None = None,
                                 rights_evidence: str = "",
                                 source_video_title: str | None = None,
                                 auto_edit_settings: dict | None = None) -> str:
        from app.services.source_store import source_store
        from app.models.schemas import SourceAsset
        from app.services.ffmpeg_service import FFmpegService
        from app.services.clip_service import ClipService
        source_id = f"youtube_{video_id}"
        source_url = canonical_watch_url(video_id)
        verified_title = title if source_video_title is None else source_video_title
        if not source_url:
            raise ValueError("INVALID_VIDEO_ID")
        target_dir = self.trends_dir / video_id
        final_mp4 = self._reusable_source(video_id)
        if not self._validate_with_ffprobe(final_mp4):
            raise ValueError("INVALID_SOURCE")

        # Check if source asset exists
        asset = source_store.get(source_id)
        if not asset and final_mp4.exists():
            try:
                metadata = FFmpegService.get_metadata(final_mp4)
                asset = SourceAsset(
                    source_id=source_id,
                    provider="youtube",
                    provider_video_id=video_id,
                    title=title,
                    source_channel=source_channel,
                    source_video_id=video_id,
                    source_video_title=verified_title,
                    source_channel_name=source_channel,
                    source_channel_id=source_channel_id,
                    rights_evidence=rights_evidence,
                    source_visibility="public",
                    source_path=str(final_mp4),
                    source_url=source_url,
                    duration=metadata.duration,
                    width=metadata.width,
                    height=metadata.height,
                    video_codec=metadata.video_codec,
                    audio_codec=metadata.audio_codec,
                    rights_status=rights_status,
                    acquisition_status="COMPLETED",
                    processing_status="READY",
                    created_at=time.time(),
                    acquired_at=time.time(),
                    thumbnail_path=None,
                    storage_lifecycle_version=1 if new_acquisition else 0,
                )
                
                # Check for thumbnail
                for ext in ['.jpg', '.webp', '.png']:
                    if (target_dir / f"thumbnail{ext}").exists():
                        asset.thumbnail_path = str(target_dir / f"thumbnail{ext}")
                        break
                        
                source_store.set(asset)
            except Exception as e:
                import logging
                logging.exception(f"Failed to create SourceAsset for {video_id}")
                raise
        elif asset:
            asset.title = title
            asset.source_channel = source_channel or asset.source_channel
            asset.source_video_id = video_id
            asset.source_video_title = verified_title
            asset.source_channel_name = source_channel or asset.source_channel_name
            asset.source_channel_id = source_channel_id or asset.source_channel_id
            asset.source_url = source_url
            asset.rights_evidence = rights_evidence or asset.rights_evidence
            asset.source_visibility = "public"
            asset.source_path = str(final_mp4)
            asset.rights_status = rights_status
            asset.acquisition_status = "COMPLETED"
            if new_acquisition:
                asset.processing_status = "READY"
                asset.storage_status = "LOCAL"
                asset.storage_lifecycle_version = 1
                asset.acquired_at = time.time()
            source_store.set(asset)

        # Check if job exists for this source
        # We can use source_id as the job_id base, or create a unique one. 
        # But idempotency requires we reuse the job if it exists.
        job_id = f"job_{source_id}"
        existing_job = ClipService.get_job(job_id)
        
        if auto_edit_settings is not None and asset:
            asset.requested_clip_count = auto_edit_settings.get("target_clip_count")
            asset.count_mode = auto_edit_settings.get("count_mode", "AUTO")
            source_store.set(asset)
        already_ready = (existing_job and existing_job.get("status") == "READY_TO_RENDER"
                         and existing_job.get("analysis_settings") == auto_edit_settings
                         and bool(existing_job.get("candidates")))
        needs_processing = ((auto_edit_settings is not None and not already_ready) or not existing_job or existing_job.get("status") == "FAILED" or
                            not existing_job.get("filepath") or
                            not Path(existing_job["filepath"]).is_file())
        if needs_processing:
            job = existing_job or ClipService.create_job(job_id, f"{title}.mp4", final_mp4)
            job["filename"] = f"{title}.mp4"
            job["filepath"] = str(final_mp4)
            job["source_id"] = source_id
            job["rights_status"] = rights_status
            job["source_title"] = title
            job["source_channel"] = source_channel or (asset.source_channel if asset else None)
            job["source_video_id"] = video_id
            job["source_video_title"] = verified_title
            job["source_channel_name"] = job["source_channel"]
            job["source_channel_id"] = source_channel_id or (asset.source_channel_id if asset else None)
            job["source_url"] = source_url
            job["source_visibility"] = "public"
            job["rights_evidence"] = rights_evidence or (asset.rights_evidence if asset else "")
            job["source_cleanup_eligible"] = bool(asset and asset.storage_lifecycle_version == 1)
            if new_acquisition:
                job["lifecycle_version"] = 1
            job["source_storage_status"] = "LOCAL"
            job["error"] = None
            # Add metadata to job
            try:
                if not asset:
                    asset = source_store.get(source_id)
                metadata = FFmpegService.get_metadata(final_mp4)
                job["metadata"] = metadata.model_dump()
                
                # Auto render flag setup
                from app.services.auto_edit_director import AutoEditDirector
                job["auto_render"] = False if auto_edit_settings is not None else True
                job["analysis_settings"] = auto_edit_settings or AutoEditDirector.settings().model_dump()
                
                from app.services.job_store import job_store as js
                js.set(job_id, job)
                
                ClipService.update_job_status(job_id, "QUEUED_ANALYSIS")
            except Exception as e:
                import logging
                logging.exception(f"Failed to register job for {job_id}")
                raise
                
        return job_id

    def _validate_with_ffprobe(self, file_path: Path) -> bool:
        import subprocess
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            return False
        try:
            result = subprocess.run(
                [config.get_ffprobe_path(), "-v", "error", "-show_entries",
                 "format=duration:stream=codec_type", "-of", "json", str(file_path)],
                capture_output=True, text=True, check=True, timeout=20,
            )
            data = json.loads(result.stdout)
            return (float(data.get("format", {}).get("duration", 0)) > 0
                    and {"video", "audio"}.issubset(
                        {stream.get("codec_type") for stream in data.get("streams", [])}))
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def _download_worker(self, dl_id: str, video_id: str, title: str,
                         rights_status: str = "UNKNOWN", rights_evidence: str = "",
                         source_video_title: str | None = None,
                         source_channel_name: str = "", source_channel_id: str = ""):
        from app.services.youtube_acquisition import acquire_public_video, AcquisitionFailure

        work_dir = self.temp_dir / dl_id
        final_mp4 = self.trends_dir / video_id / "source.mp4"
        moved = False
        attempted_strategies = []
        last_disk_check_bytes = 0
        checked_estimate = False

        def set_strategy(strategy: str):
            attempted_strategies.append(strategy)
            self._set_status(dl_id, video_id, status="DOWNLOADING", strategy=strategy,
                             attempted_strategies=attempted_strategies.copy())

        def on_progress(data: dict):
            nonlocal last_disk_check_bytes, checked_estimate
            if data.get("status") != "downloading":
                return
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            if total and not checked_estimate:
                storage_lifecycle.require_acquisition_space(
                    estimated_source_bytes=max(0, total - downloaded))
                checked_estimate = True
            if downloaded < last_disk_check_bytes:
                last_disk_check_bytes = 0
            if downloaded - last_disk_check_bytes >= 16 * 1024 * 1024:
                storage_lifecycle.require_space()
                last_disk_check_bytes = downloaded
            if total:
                with self.lock:
                    state = self.active_downloads.get(dl_id)
                    if state:
                        state["progress"] = min(1.0, data.get("downloaded_bytes", 0) / total)
                        state["eta_seconds"] = data.get("eta")

        try:
            candidate, info, strategy = acquire_public_video(
                video_id, work_dir, self.cache_dir, self._validate_with_ffprobe,
                set_strategy, on_progress,
            )
            storage_lifecycle.require_space()
            final_mp4.parent.mkdir(parents=True, exist_ok=True)
            candidate.replace(final_mp4)
            moved = True
            known_title = source_video_title or info.get("title") or ""
            known_channel_id = source_channel_id or info.get("channel_id") or ""
            (final_mp4.parent / "metadata.json").write_text(json.dumps({
                "id": video_id, "title": title,
                "source_video_title": known_title,
                "duration": info.get("duration"),
                "channel": source_channel_name or info.get("uploader"),
                "channel_id": known_channel_id,
                "source_url": canonical_watch_url(video_id),
                "rights_status": rights_status, "rights_evidence": rights_evidence,
                "strategy": strategy,
            }))
            job_id = self._register_source_and_job(
                video_id, title, rights_status, new_acquisition=True,
                source_channel=source_channel_name or info.get("channel") or info.get("uploader"),
                source_channel_id=known_channel_id, rights_evidence=rights_evidence,
                source_video_title=known_title,
                auto_edit_settings=self.get_status(dl_id).get("auto_edit_settings"))
            final_state = {"status": "COMPLETED", "progress": 1.0,
                           "job_id": job_id, "error": None, "error_code": None,
                           "strategy": strategy}
        except AcquisitionFailure as exc:
            final_state = {"status": exc.status, "error": exc.code,
                           "error_code": exc.code,
                           "attempt_diagnostics": exc.attempts,
                           "yt_dlp_exit_status": None}
        except InsufficientStorage:
            final_state = {"status": "FAILED", "error": "DISK_FULL",
                           "error_code": "DISK_FULL"}
        except Exception as exc:
            logger.error("YouTube acquisition worker failed: video=%s error_type=%s",
                         video_id, type(exc).__name__)
            final_state = {"status": "FAILED", "error": "INTERNAL_ERROR",
                           "error_code": "INTERNAL_ERROR"}
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if moved and final_state["status"] != "COMPLETED":
                final_mp4.unlink(missing_ok=True)
        self._set_status(dl_id, video_id, **final_state)
        from app.services.job_store import job_store
        registered = job_store.get(final_state.get("job_id", "")) if final_state.get("job_id") else None
        if final_state["status"] != "COMPLETED" or not registered or registered.get("status") not in (
            "QUEUED_ANALYSIS", "PREPARING_ANALYSIS_MEDIA", "GENERATING_PROXY",
            "UPLOADING_TO_GEMINI", "WAITING_FOR_GEMINI", "VALIDATING_CANDIDATES",
            "QUEUED_RENDER", "RENDERING"):
            heavy_gate.release(f"job_youtube_{video_id}")

download_service = DownloadService(config.DATA_ROOT)
