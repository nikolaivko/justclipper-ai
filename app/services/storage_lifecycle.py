"""Metadata-led cleanup of large files on the persistent volume."""

import logging
import copy
import hashlib
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from app.config import config
from app.services.job_store import job_store
from app.services.source_store import source_store

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {
    "QUEUED_ANALYSIS", "PREPARING_ANALYSIS_MEDIA", "GENERATING_PROXY",
    "UPLOADING_TO_GEMINI", "WAITING_FOR_GEMINI", "VALIDATING_CANDIDATES",
    "QUEUED_RENDER", "RENDERING", "RUNNING_LOCAL_ANALYSIS",
}


class InsufficientStorage(Exception):
    pass


class StorageLifecycleManager:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or config.DATA_ROOT).resolve()

    def get_disk_usage(self) -> dict:
        total, used, free = shutil.disk_usage(self.root)
        return {"total": total, "used": used, "free": free}

    def can_start_heavy_job(self) -> bool:
        return self.get_disk_usage()["free"] >= config.MIN_FREE_DISK_GB * 1024 ** 3

    def require_space(self) -> None:
        if not self.can_start_heavy_job():
            raise InsufficientStorage("INSUFFICIENT_STORAGE")

    def processing_floor_bytes(self, job: dict | None = None) -> int:
        """Uploaded files can use a smaller floor; acquired sources keep the old one."""
        floor = (config.DIRECT_UPLOAD_MIN_FREE_GB if job and
                 job.get("storage_profile") == "DIRECT_UPLOAD" else config.MIN_FREE_DISK_GB)
        return int(max(0, floor) * 1024 ** 3)

    def require_processing_space(self, job: dict | None = None,
                                 working_bytes: int = 0) -> None:
        if self.get_disk_usage()["free"] < self.processing_floor_bytes(job) + max(0, working_bytes):
            raise InsufficientStorage("INSUFFICIENT_STORAGE")

    def require_acquisition_space(self, estimated_source_bytes: int | None = None) -> None:
        """Keep the hard floor plus room for acquisition and processing files."""
        reserve = max(0, config.ACQUISITION_WORKING_RESERVE_GB) * 1024 ** 3
        if estimated_source_bytes and estimated_source_bytes > 0:
            reserve = max(reserve, estimated_source_bytes * 2)
        required = config.MIN_FREE_DISK_GB * 1024 ** 3 + reserve
        if self.get_disk_usage()["free"] < required:
            raise InsufficientStorage("INSUFFICIENT_STORAGE")

    def require_upload_space(self, remaining_bytes: int) -> None:
        """Reserve the remaining direct upload, the hard floor and processing room."""
        if remaining_bytes < 0:
            raise ValueError("INVALID_UPLOAD_SIZE")
        required = ((config.DIRECT_UPLOAD_MIN_FREE_GB +
                     max(0, config.UPLOAD_WORKING_RESERVE_GB)) * 1024 ** 3 +
                    remaining_bytes)
        if self.get_disk_usage()["free"] < required:
            raise InsufficientStorage("INSUFFICIENT_STORAGE")

    def _safe_file(self, value: str | Path, *allowed_roots: Path) -> Path:
        path = Path(value)
        cursor = path.absolute()
        while cursor != cursor.parent:
            if cursor.is_symlink():
                raise ValueError("UNSAFE_STORAGE_PATH")
            if cursor == self.root:
                break
            cursor = cursor.parent
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("UNSAFE_STORAGE_PATH")
        if resolved.is_relative_to(self.root / "youtube") or resolved.is_relative_to(self.root / "config"):
            raise ValueError("UNSAFE_STORAGE_PATH")
        if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
            raise ValueError("UNSAFE_STORAGE_PATH")
        if resolved.is_dir():
            raise ValueError("UNSAFE_STORAGE_PATH")
        return resolved

    def _delete(self, value: str | Path, *allowed_roots: Path) -> bool:
        path = self._safe_file(value, *allowed_roots)
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            logger.exception("Storage cleanup failed for an allowed file")
            return False

    def _source_file(self, value: str | Path, job_id: str) -> Path:
        trends = self.root / "sources" / "trends" / "youtube"
        path = self._safe_file(value, self.root / "input", trends)
        if path.is_relative_to(trends):
            if (path.name != "source.mp4" or path.parent.parent != trends or
                    not re.fullmatch(r"[A-Za-z0-9_-]{11}", path.parent.name)):
                raise ValueError("UNSAFE_STORAGE_PATH")
        elif (path.parent != self.root / "input" or
              not path.name.startswith(f"{job_id}_") or
              path.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".m4v")):
            raise ValueError("UNSAFE_STORAGE_PATH")
        return path

    def _render_file(self, value: str | Path, job_id: str, rank: int) -> Path:
        path = self._safe_file(value, self.root / "output")
        if (path.parent != self.root / "output" or
                not path.name.startswith(f"{job_id}_clip_{rank:02d}_") or
                path.suffix.lower() != ".mp4"):
            raise ValueError("UNSAFE_STORAGE_PATH")
        return path

    def _ready_source(self, job: dict, jobs: list[dict]) -> Path | None:
        """Only a uniquely owned completed source with every requested final can go."""
        job_id = job.get("id", "")
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", job_id) or
                job.get("status") != "COMPLETED" or not job.get("filepath")):
            return None
        if job.get("bulk_publish_status") == "RUNNING":
            return None
        requested = (job.get("render_request") or {}).get("candidates") or []
        renders = job.get("renders") or []
        requested_ranks = [item.get("rank") for item in requested]
        render_ranks = [item.get("rank") for item in renders]
        if (not requested_ranks or
                not all(type(rank) is int for rank in requested_ranks + render_ranks) or
                len(set(requested_ranks)) != len(requested_ranks) or
                sorted(requested_ranks) != sorted(render_ranks) or
                not all(render.get("success") for render in renders) or
                any(render.get("publish_status") == "UPLOADING" for render in renders)):
            return None
        try:
            source = self._source_file(job["filepath"], job_id)
            if not source.is_file():
                return None
        except (ValueError, OSError):
            return None
        for other in jobs:
            if other.get("id") != job_id and other.get("filepath"):
                try:
                    if self._safe_file(other["filepath"], self.root / "input",
                                       self.root / "sources" / "trends" / "youtube") == source:
                        return None
                except ValueError:
                    continue
        expected_source_id = job.get("source_id") or (
            job_id[4:] if job_id.startswith("job_youtube_") else None)
        for asset in source_store.get_all():
            if asset.source_path and asset.source_id != expected_source_id:
                try:
                    if self._safe_file(asset.source_path, self.root / "input",
                                       self.root / "sources" / "trends" / "youtube") == source:
                        return None
                except ValueError:
                    continue
        for render in renders:
            output = render.get("output_path")
            if (not output and render.get("storage_status") == "CLEANED" and
                    render.get("publish_status") == "UPLOADED" and render.get("youtube_video_id")):
                continue
            try:
                from app.services.youtube_publisher import youtube_publisher
                if not output or not youtube_publisher.validate_file(
                        self._render_file(output, job_id, render["rank"])):
                    return None
            except (ValueError, KeyError, TypeError):
                return None
        return source

    @staticmethod
    def _digest(path: Path) -> bytes:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.digest()

    def _verified_duplicate(self, path: Path, job: dict, jobs: list[dict]) -> bool:
        job_id = job.get("id", "")
        if job.get("status") in ACTIVE_STATUSES or not job.get("filepath"):
            return False
        try:
            candidate = self._source_file(path, job_id)
            canonical = self._source_file(job["filepath"], job_id)
            if (candidate.parent != self.root / "input" or candidate == canonical or
                    not candidate.is_file() or not canonical.is_file() or
                    candidate.stat().st_size != canonical.stat().st_size):
                return False
            for other in jobs:
                if other.get("filepath") == str(candidate):
                    return False
            if any(asset.source_path == str(candidate) for asset in source_store.get_all()):
                return False
            return self._digest(candidate) == self._digest(canonical)
        except (ValueError, OSError):
            return False

    def plan_reconciliation(self, now: float | None = None) -> list[dict]:
        """Read-only list of files whose stored state proves cleanup is safe."""
        now = time.time() if now is None else now
        jobs = job_store.get_all()
        actions = []
        render_references = {}
        for job in jobs:
            for render in job.get("renders") or []:
                if render.get("output_path"):
                    path = render["output_path"]
                    render_references[path] = render_references.get(path, 0) + 1
        for job in jobs:
            job_id = job.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                continue
            source = self._ready_source(job, jobs)
            if source and (job.get("lifecycle_version") == 1 and
                           job.get("source_cleanup_eligible") or
                           job.get("lifecycle_version") != 1):
                actions.append({"kind": "source", "job_id": job_id,
                                "path": source, "bytes": source.stat().st_size})
            if job.get("status") in ACTIVE_STATUSES or job.get("bulk_publish_status") == "RUNNING":
                continue
            for render in job.get("renders") or []:
                output = render.get("output_path")
                rank = render.get("rank")
                if (not output or render_references.get(output) != 1 or
                        not isinstance(rank, int) or not render.get("success")):
                    continue
                published = (render.get("publish_status") == "UPLOADED" and
                             bool(render.get("youtube_video_id")))
                expired = (job.get("status") == "COMPLETED" and
                           not render.get("publish_status") and
                           now - (render.get("completed_at") or job.get("completed_at") or now)
                           >= config.FINAL_OUTPUT_RETENTION_HOURS * 3600)
                if not published and not expired:
                    continue
                try:
                    path = self._render_file(output, job_id, rank)
                    if path.is_file():
                        actions.append({"kind": "uploaded" if published else "expired",
                                        "job_id": job_id, "rank": rank, "path": path,
                                        "bytes": path.stat().st_size})
                    elif published:
                        actions.append({"kind": "uploaded_missing", "job_id": job_id,
                                        "rank": rank, "path": path, "bytes": 0})
                except (ValueError, OSError):
                    continue
            if job.get("status") in ACTIVE_STATUSES:
                continue
            temp = self.root / "temp"
            for path in temp.glob(f"{job_id}_*"):
                try:
                    checked = self._safe_file(path, temp)
                    if (checked.is_file() and now - checked.stat().st_mtime >= 3600 and
                            checked.suffix.lower() in (".mp4", ".wav", ".m4a", ".json", ".txt")):
                        actions.append({"kind": "intermediate", "job_id": job_id,
                                        "path": checked, "bytes": checked.stat().st_size})
                except (ValueError, OSError):
                    continue
            for prefix in ("temp", "concat", "sendcmd", "blank", "subs", "sub_overlay"):
                for path in (self.root / "output").glob(f"{prefix}_{job_id}_clip_*"):
                    try:
                        checked = self._safe_file(path, self.root / "output")
                        if (checked.is_file() and now - checked.stat().st_mtime >= 3600 and
                                checked.suffix.lower() in (".mp4", ".png", ".txt", ".ffconcat")):
                            actions.append({"kind": "intermediate", "job_id": job_id,
                                            "path": checked, "bytes": checked.stat().st_size})
                    except (ValueError, OSError):
                        continue
            for path in (self.root / "input").glob(f"{job_id}_*"):
                if self._verified_duplicate(path, job, jobs):
                    actions.append({"kind": "duplicate", "job_id": job_id,
                                    "path": path, "bytes": path.stat().st_size})

        # Only an explicit terminal acquisition record proves that a UUID work
        # directory is abandoned. Unknown directories remain untouched.
        terminal = {"FAILED", "ACQUISITION_BLOCKED", "AUTH_REQUIRED",
                    "RIGHTS_BLOCKED", "UNAVAILABLE"}
        for status_file in (self.root / "sources" / "trends" / "youtube").glob("*/acquisition.json"):
            try:
                self._safe_file(status_file, self.root / "sources" / "trends" / "youtube")
                saved = json.loads(status_file.read_text())
                dl_id = saved.get("id", "")
                uuid.UUID(dl_id)
                if saved.get("status") not in terminal:
                    continue
                work_dir = self.root / "temp" / "downloads" / dl_id
                if work_dir.is_symlink():
                    continue
                for attempt in ("1", "2"):
                    if (work_dir / attempt).is_symlink():
                        continue
                    for path in (work_dir / attempt).glob("source.*"):
                        checked = self._safe_file(path, self.root / "temp" / "downloads")
                        if (checked.is_file() and now - checked.stat().st_mtime >= 3600 and
                                checked.name.startswith("source.") and
                                checked.suffix.lower() in (".mp4", ".m4a", ".webm", ".part", ".ytdl")):
                            actions.append({"kind": "partial", "path": checked,
                                            "bytes": checked.stat().st_size})
            except (ValueError, OSError, TypeError):
                continue
        return actions

    def cleanup_job_intermediates(self, job_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id or ""):
            return
        temp = self.root / "temp"
        for path in temp.glob(f"{job_id}_*"):
            if path.is_file():
                try:
                    self._delete(path, temp)
                except ValueError:
                    logger.warning("Skipped unsafe intermediate")
        output = self.root / "output"
        for prefix in ("temp", "concat", "sendcmd", "blank", "subs", "sub_overlay"):
            for path in output.glob(f"{prefix}_{job_id}_clip_*"):
                if path.is_file():
                    try:
                        self._delete(path, output)
                    except ValueError:
                        logger.warning("Skipped unsafe intermediate")
        for path in output.glob(f"{job_id}_clip_*.ass"):
            if path.is_file():
                try:
                    self._delete(path, output)
                except ValueError:
                    logger.warning("Skipped unsafe intermediate")

    def cleanup_source(self, job_id: str) -> bool:
        job = job_store.get(job_id)
        if not job or (job.get("lifecycle_version") == 1 and
                       not job.get("source_cleanup_eligible")):
            return False
        source_path = job.get("filepath")
        if not source_path:
            return job.get("source_storage_status") == "CLEANED"
        source = self._ready_source(job, job_store.get_all())
        if source is None:
            return False
        try:
            deleted = self._delete(source, self.root / "input",
                                   self.root / "sources" / "trends" / "youtube")
        except ValueError:
            return False
        if not deleted:
            return False
        job["filepath"] = None
        job["source_storage_status"] = "CLEANED"
        job["source_cleaned_at"] = time.time()
        job_store.set(job_id, job, durable=True)
        source_id = job.get("source_id") or (
            job_id[4:] if job_id.startswith("job_youtube_") else None)
        if source_id:
            asset = source_store.get(source_id)
            if asset:
                asset.source_path = ""
                asset.storage_status = "CLEANED"
                asset.processing_status = "COMPLETED"
                source_store.set(asset)
        self.cleanup_job_intermediates(job_id)
        return True

    def cleanup_render(self, job_id: str, rank: int, *, expired: bool = False) -> bool:
        job = job_store.get(job_id)
        if not job or job.get("status") in ACTIVE_STATUSES:
            return False
        render = next((r for r in job.get("renders", []) if r.get("rank") == rank), None)
        if not render or not render.get("success"):
            return False
        published = render.get("publish_status") == "UPLOADED" and bool(render.get("youtube_video_id"))
        if not published and not expired:
            return False
        if expired and not published:
            age = time.time() - (render.get("completed_at") or job.get("completed_at") or time.time())
            if (job.get("status") != "COMPLETED" or render.get("publish_status") or
                    age < config.FINAL_OUTPUT_RETENTION_HOURS * 3600):
                return False
        output = render.get("output_path")
        if not output:
            return render.get("storage_status") == "CLEANED"
        if any(other.get("id") != job_id and any(
                item.get("output_path") == output for item in other.get("renders") or [])
               for other in job_store.get_all()):
            return False
        try:
            path = self._render_file(output, job_id, rank)
            if expired and not published:
                from app.services.youtube_publisher import youtube_publisher
                if not youtube_publisher.validate_file(path):
                    return False
            deleted = self._delete(path, self.root / "output")
        except ValueError:
            return False
        if not deleted:
            return False
        render["output_path"] = None
        render["storage_status"] = "CLEANED"
        render["cleaned_at"] = time.time()
        render["cleanup_reason"] = "PUBLISHED" if published else "EXPIRED"
        job_store.set(job_id, job, durable=True)
        return True

    def discard_failed_publication_renders(
            self, job_id: str, ranks: tuple[int, ...], allowed_errors: set[str]) -> dict:
        """Honor an explicit discard request for one fully verified job only."""
        from app.services.youtube_publish_service import publishing_lock

        summary = {"deleted_files": 0, "reclaimed_bytes": 0}
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", job_id) or not ranks or
                any(type(rank) is not int or rank < 1 for rank in ranks) or
                len(set(ranks)) != len(ranks)):
            return summary
        if not job_store.get(job_id):
            return summary
        with publishing_lock(job_id):
            job = job_store.get(job_id)
            if (not job or job.get("id") != job_id or
                    job.get("lifecycle_version") != 1 or
                    job.get("status") != "COMPLETED" or
                    job.get("bulk_publish_status") == "RUNNING" or
                    job.get("source_storage_status") != "CLEANED"):
                return summary
            renders = job.get("renders") or []
            if (len(renders) != len(ranks) or
                    any(type(render.get("rank")) is not int for render in renders) or
                    {render.get("rank") for render in renders} != set(ranks)):
                return summary
            references = {render.get("output_path") for other in job_store.get_all()
                          if other.get("id") != job_id
                          for render in other.get("renders") or []
                          if render.get("output_path")}
            pending = []
            for render in renders:
                if (not render.get("success") or render.get("publish_status") != "FAILED" or
                        render.get("publish_error_code") not in allowed_errors or
                        render.get("youtube_video_id")):
                    return summary
                if render.get("storage_status") == "CLEANED":
                    if (render.get("output_path") or
                            render.get("cleanup_reason") != "USER_DISCARDED"):
                        return summary
                    continue
                output = render.get("output_path")
                if (render.get("storage_status") != "LOCAL" or not output or
                        output in references or
                        render.get("cleanup_reason") not in (None, "USER_DISCARDED")):
                    return summary
                try:
                    path = self._render_file(output, job_id, render["rank"])
                    recover_pending = (render.get("cleanup_reason") == "USER_DISCARDED" and
                                       render.get("discard_pending_at"))
                    if (not path.is_file() and not recover_pending or
                            path.is_file() and path.stat().st_size <= 0):
                        return summary
                except (ValueError, OSError, KeyError):
                    return summary
                pending.append((render, path))

            for render, path in pending:
                size = path.stat().st_size if path.is_file() else 0
                render["cleanup_reason"] = "USER_DISCARDED"
                render["discard_pending_at"] = time.time()
                job_store.set(job_id, job, durable=True)
                if not self._delete(path, self.root / "output"):
                    break
                render["output_path"] = None
                render["storage_status"] = "CLEANED"
                render["cleaned_at"] = time.time()
                render.pop("discard_pending_at", None)
                job_store.set(job_id, job, durable=True)
                if size:
                    summary["deleted_files"] += 1
                    summary["reclaimed_bytes"] += size
        return summary

    def cleanup_expired_outputs(self, now: float | None = None) -> int:
        now = now or time.time()
        count = 0
        for job in job_store.get_all():
            if job.get("status") in ACTIVE_STATUSES:
                continue
            if (job.get("bulk_publish_status") == "RUNNING" and
                    now - (job.get("bulk_publish_started_at") or 0) < 2 * 3600):
                continue
            if any(render.get("publish_status") == "UPLOADING" for render in job.get("renders", [])):
                continue
            for render in job.get("renders", []):
                if not render.get("output_path") or render.get("publish_status") == "UPLOADING":
                    continue
                if render.get("publish_status") == "UPLOADED" and render.get("youtube_video_id"):
                    count += self.cleanup_render(job["id"], render["rank"])
                elif (not render.get("publish_status") and
                      now - (render.get("completed_at") or job.get("completed_at") or now)
                      >= config.FINAL_OUTPUT_RETENTION_HOURS * 3600):
                    count += self.cleanup_render(job["id"], render["rank"], expired=True)
        return count

    def cleanup_failed_jobs(self, now: float | None = None) -> int:
        now = now or time.time()
        count = 0
        for job in job_store.get_all():
            if (job.get("lifecycle_version") != 1 or job.get("status") != "FAILED"
                    or now - (job.get("failed_at") or now) < config.FAILED_JOB_RETENTION_HOURS * 3600):
                continue
            if any(r.get("publish_status") == "UPLOADING" for r in job.get("renders", [])):
                continue
            self.cleanup_job_intermediates(job["id"])
            source = job.get("filepath") if job.get("source_cleanup_eligible") else None
            try:
                source_deleted = bool(source and self._delete(self._source_file(source, job["id"]), self.root / "input", self.root / "sources" / "trends" / "youtube"))
            except ValueError:
                source_deleted = False
            if source_deleted:
                job["filepath"] = None
                job["source_storage_status"] = "CLEANED"
                job["source_cleaned_at"] = now
                count += 1
                source_id = job.get("source_id")
                asset = source_store.get(source_id) if source_id else None
                if asset:
                    asset.source_path = ""
                    asset.storage_status = "CLEANED"
                    asset.processing_status = "FAILED"
                    source_store.set(asset)
            for render in job.get("renders", []):
                if render.get("publish_status") in ("FAILED", "REAUTH_REQUIRED", "UPLOADING"):
                    continue
                output = render.get("output_path")
                try:
                    output_deleted = bool(output and self._delete(self._render_file(output, job["id"], render["rank"]), self.root / "output"))
                except ValueError:
                    output_deleted = False
                if output_deleted:
                    render["output_path"] = None
                    render["storage_status"] = "CLEANED"
                    render["cleaned_at"] = now
                    count += 1
            job_store.set(job["id"], job)
        return count

    def reconcile_storage(self) -> dict:
        """Execute only current, revalidated dry-run candidates; safe to repeat."""
        summary = {"deleted_files": 0, "reclaimed_bytes": 0}
        for action in sorted(self.plan_reconciliation(),
                             key=lambda item: 0 if item["kind"] == "duplicate" else 1):
            path = action["path"]
            try:
                before = path.stat().st_size if path.is_file() else 0
                kind = action["kind"]
                if kind == "source":
                    deleted = self.cleanup_source(action["job_id"])
                elif kind in ("uploaded", "uploaded_missing", "expired"):
                    deleted = self.cleanup_render(action["job_id"], action["rank"],
                                                  expired=kind == "expired")
                elif kind == "intermediate":
                    job = job_store.get(action["job_id"])
                    deleted = bool(job and job.get("status") not in ACTIVE_STATUSES and
                                   time.time() - path.stat().st_mtime >= 3600 and
                                   self._delete(path, self.root / "temp", self.root / "output"))
                elif kind == "duplicate":
                    job = job_store.get(action["job_id"])
                    deleted = bool(job and self._verified_duplicate(
                        path, job, job_store.get_all()) and self._delete(path, self.root / "input"))
                elif kind == "partial":
                    # Replanning catches a status change or a path replaced by a symlink.
                    current = {(a["kind"], a["path"]) for a in self.plan_reconciliation()}
                    deleted = bool((kind, path) in current and
                                   self._delete(path, self.root / "temp" / "downloads"))
                else:
                    deleted = False
                if deleted and before and not path.exists():
                    summary["deleted_files"] += 1
                    summary["reclaimed_bytes"] += before
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("Skipped storage cleanup candidate: kind=%s", action["kind"])
        logger.info("storage_reconciliation deleted_files=%s reclaimed_bytes=%s",
                    summary["deleted_files"], summary["reclaimed_bytes"])
        return summary

    def reconcile_stale_jobs(self) -> int:
        """On startup: find jobs in an active processing state that have no running worker.
        These are orphaned by a server restart. Per user policy: delete everything immediately
        — source video, proxy, renders, and job record. Do not leave FAILED tombstones."""
        from app.services.job_store import job_store
        from app.services.heavy_gate import heavy_gate
        import shutil

        count = 0
        for job in job_store.get_all():
            status = job.get("status")
            if status in ACTIVE_STATUSES and status not in ["QUEUED_ANALYSIS", "QUEUED_RENDER"]:
                job_id = job.get("id")
                # Gate is empty on startup (in-memory only), so any mid-processing status is stale
                _logger = logging.getLogger(__name__)
                _logger.info(f"STALE_JOB_PURGE job_id={job_id} status={status} — deleting all associated files")

                # 1. Intermediates (proxy, audio, etc.)
                self.cleanup_job_intermediates(job_id)

                # 2. Source video
                source_path = job.get("filepath")
                if source_path:
                    src = Path(source_path)
                    if src.exists():
                        # Delete the parent folder (e.g. data/sources/trends/youtube/VIDEO_ID/)
                        try:
                            parent = src.parent
                            shutil.rmtree(parent, ignore_errors=True)
                        except Exception:
                            src.unlink(missing_ok=True)

                # 3. Rendered clips (if any were generated before the crash)
                for render in job.get("renders", []):
                    render_path = render.get("filepath")
                    if render_path:
                        Path(render_path).unlink(missing_ok=True)
                    thumb_path = render.get("thumbnail")
                    if thumb_path:
                        Path(thumb_path).unlink(missing_ok=True)

                # 4. Delete the job record itself — no tombstone left
                job_store.delete(job_id)
                count += 1

        return count


    def prepare_for_new_video(self) -> dict:
        """Apply existing retention and metadata-led cleanup before acquisition."""
        stale = self.reconcile_stale_jobs()
        failed = self.cleanup_failed_jobs()
        expired = self.cleanup_expired_outputs()
        reconciled = self.reconcile_storage()
        return {"failed_files": failed, "expired_files": expired, "stale_jobs": stale, **reconciled}

    def reconcile_legacy_attribution(self) -> int:
        """Recover known lightweight history from SourceStore, including cleaned jobs."""
        from app.services.youtube_metadata import build_youtube_metadata
        from app.services.youtube_source import canonical_watch_url, source_attribution

        updated = 0
        for job in job_store.get_all():
            job_id = job.get("id", "")
            match = re.fullmatch(r"job_youtube_([A-Za-z0-9_-]{11})", job_id)
            if not match:
                continue
            video_id = match.group(1)
            source_id = f"youtube_{video_id}"
            asset = source_store.get(source_id)
            if not asset or asset.provider != "youtube" or asset.provider_video_id != video_id:
                continue
            url = canonical_watch_url(video_id)
            if (not url or (job.get("source_id") and job["source_id"] != source_id) or
                    (job.get("source_video_id") and job["source_video_id"] != video_id)):
                continue
            if (not asset.source_video_id or not asset.source_video_title or
                    asset.source_url != url):
                asset.source_video_id = asset.source_video_id or video_id
                asset.source_video_title = asset.source_video_title or asset.title
                asset.source_channel_name = asset.source_channel_name or asset.source_channel
                asset.source_url = url
                source_store.set(asset)
            original = copy.deepcopy(job)
            job["source_id"] = source_id
            job["source_video_id"] = video_id
            job["source_video_title"] = job.get("source_video_title") or asset.source_video_title or asset.title
            job["source_channel_name"] = (job.get("source_channel_name") or
                                          asset.source_channel_name or asset.source_channel or "")
            job["source_channel_id"] = job.get("source_channel_id") or asset.source_channel_id or ""
            job["source_url"] = url
            job["rights_status"] = job.get("rights_status") or asset.rights_status
            job["rights_evidence"] = job.get("rights_evidence") or asset.rights_evidence or ""
            attribution = source_attribution(job)
            for render in job.get("renders") or []:
                if not render.get("success"):
                    continue
                for key, value in attribution.items():
                    if value and not render.get(key):
                        render[key] = value
                if (not render.get("youtube_metadata") and
                        render.get("publish_status") != "UPLOADED"):
                    try:
                        render["youtube_metadata"] = build_youtube_metadata(job, render)
                    except (ValueError, TypeError, KeyError):
                        logger.warning("Skipped malformed legacy render metadata")
            if job != original:
                job_store.set(job_id, job, durable=True)
                updated += 1
        logger.info("storage_attribution_reconciliation updated_jobs=%s", updated)
        return updated


storage_lifecycle = StorageLifecycleManager()
