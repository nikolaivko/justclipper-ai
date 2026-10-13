"""Serialized, idempotent per-render publishing for manual user actions."""

import fcntl
import logging
import re
import time
from contextlib import contextmanager

from app.models.schemas import YouTubeMetadata
from app.services.job_store import job_store
from app.services.render_resolution import RenderResolutionError, resolve_render
from app.services.storage_lifecycle import storage_lifecycle
from app.services.youtube_metadata import build_youtube_metadata, is_placeholder_title
from app.services.youtube_publisher import youtube_publisher
from app.services.youtube_service import youtube_service
from app.services.source_rights_service import SourceRightsService
from app.services.youtube_source import source_attribution

logger = logging.getLogger(__name__)
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
RIGHTS_NOT_CHECKED = object()
PUBLISHING_LIMIT_CODES = frozenset({
    "QUOTA_EXCEEDED", "API_QUOTA_EXCEEDED", "CHANNEL_UPLOAD_LIMIT", "RATE_LIMITED",
})
PARTIAL_RENDER_ERRORS = frozenset({
    "INCOMPLETE_RENDER_COUNT", "RENDER_FAILED", "DISK_FULL",
})


def _publishable_job(job: dict) -> bool:
    return (job.get("status") == "COMPLETED" or
            (job.get("status") == "FAILED" and
             job.get("error") in PARTIAL_RENDER_ERRORS and
             any(render.get("success") for render in job.get("renders", []))))


@contextmanager
def publishing_lock(job_id: str, *, blocking: bool = True):
    if not JOB_ID_RE.fullmatch(job_id):
        raise RenderResolutionError("JOB_NOT_FOUND", 404)
    lock_path = job_store.jobs_dir / f"{job_id}.publish.lock"
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RenderResolutionError("JOB_BUSY", 409) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _result(rank: int, status: str, **extra) -> dict:
    return {"rank": rank, "status": status, **extra}


def _mark_failure(job: dict, render: dict, code: str) -> dict:
    render["publish_status"] = "REAUTH_REQUIRED" if code == "REAUTH_REQUIRED" else "FAILED"
    render["publish_error_code"] = code
    render["publish_error"] = code
    job_store.set(job["id"], job)
    return _result(render["rank"], render["publish_status"], error_code=code)


def _publication_rights_code(job: dict) -> str | None:
    source_id = job.get("source_id") or (
        job["id"][4:] if job.get("id", "").startswith("job_youtube_") else "")
    if not source_id.startswith("youtube_"):
        return None
    video_id = source_id.removeprefix("youtube_")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return "RIGHTS_UNVERIFIED"
    try:
        verified = youtube_service.get_video_rights(video_id)
    except Exception:
        verified = None
    if not verified:
        return "RIGHTS_UNVERIFIED"
    if verified.get("rights_status") == "RESTRICTED":
        return "RIGHTS_BLOCKED"
    if verified.get("privacy_status") != "public" or verified.get("upload_status") != "processed":
        return "VIDEO_UNAVAILABLE"
    if not SourceRightsService.allows_public_acquisition(
            verified.get("rights_status"), public_verified=True):
        return "RIGHTS_UNVERIFIED"
    return None


def publish_one_locked(job_id: str, rank: int, rights_code=RIGHTS_NOT_CHECKED) -> dict:
    """Call while holding publishing_lock; never publish on render completion."""
    job = job_store.get(job_id)
    if not job or job.get("id") != job_id:
        return _result(rank, "FAILED", error_code="JOB_NOT_FOUND")
    if not _publishable_job(job):
        return _result(rank, "FAILED", error_code="JOB_NOT_COMPLETED")
    render = next((item for item in job.get("renders", []) if item.get("rank") == rank), None)
    if render is None:
        return _result(rank, "FAILED", error_code="RENDER_NOT_FOUND")
    if render.get("publish_status") == "UPLOADED" and render.get("youtube_video_id"):
        return _result(rank, "ALREADY_UPLOADED", video_id=render["youtube_video_id"],
                       privacy_status=render.get("youtube_privacy_status"))
    if not render.get("success"):
        return _result(rank, "SKIPPED", error_code="RENDER_NOT_COMPLETED")
    if render.get("publish_status") == "UPLOADING":
        return _result(rank, "FAILED", error_code="UPLOAD_IN_PROGRESS")
    if rights_code is RIGHTS_NOT_CHECKED:
        rights_code = _publication_rights_code(job)
    if rights_code:
        return _mark_failure(job, render, rights_code)

    try:
        _, _, path = resolve_render(job_id, rank)
    except RenderResolutionError as exc:
        return _mark_failure(job, render, exc.code)
    if not youtube_publisher.validate_file(path):
        return _mark_failure(job, render, "INVALID_OUTPUT")

    metadata = render.get("youtube_metadata")
    try:
        metadata = YouTubeMetadata.model_validate(metadata).model_dump() if metadata else None
    except ValueError:
        metadata = None
    if metadata is None:
        if source_attribution(job):
            return _mark_failure(job, render, "ATTRIBUTION_MISSING")
        metadata = build_youtube_metadata(job, render)
        render["youtube_metadata"] = metadata
        job_store.set(job_id, job)

    if is_placeholder_title(metadata.get("title")) or render.get("metadata_status") == "REVIEW_REQUIRED":
        return _mark_failure(job, render, "TITLE_REVIEW_REQUIRED")

    attribution = source_attribution(job)
    if attribution and (metadata.get("source_video_id") != attribution["source_video_id"] or
                        metadata.get("source_url") != attribution["source_url"] or
                        attribution["source_url"] not in metadata.get("description", "") or
                        "⚠️ Copyright & Source Notice:" not in metadata.get("description", "")):
        return _mark_failure(job, render, "ATTRIBUTION_MISSING")

    render["publish_status"] = "UPLOADING"
    render["publish_error"] = None
    render["publish_error_code"] = None
    render["publish_started_at"] = time.time()
    job_store.set(job_id, job)
    try:
        result = youtube_publisher.publish_video(
            file_path=path,
            title=metadata["title"],
            description=metadata["description"],
            tags=metadata["tags"],
        )
    except Exception:
        logger.exception("YouTube publishing failed for job %s render %s", job_id, rank)
        result = {"status": "FAILED", "error": "UPLOAD_FAILED"}

    if result.get("status") == "UPLOADED" and result.get("video_id"):
        render["publish_status"] = "UPLOADED"
        render["youtube_video_id"] = result["video_id"]
        render["youtube_privacy_status"] = result.get("privacy_status")
        render["published_at"] = result.get("published_at") or time.time()
        job_store.set(job_id, job, durable=True)
        persisted = job_store.get(job_id)
        saved = next((item for item in (persisted or {}).get("renders", [])
                     if item.get("rank") == rank), None)
        if not saved or saved.get("publish_status") != "UPLOADED" or saved.get("youtube_video_id") != result["video_id"]:
            logger.error("Could not verify persisted YouTube ID for job %s render %s", job_id, rank)
            return _result(rank, "FAILED", error_code="PERSISTENCE_FAILED")
        try:
            storage_lifecycle.cleanup_render(job_id, rank)
        except ValueError:
            logger.exception("Refused unsafe render cleanup for job %s render %s", job_id, rank)
        return _result(rank, "UPLOADED", video_id=result["video_id"],
                       privacy_status=result.get("privacy_status"))

    code = result.get("error") if result.get("error") in (
        PUBLISHING_LIMIT_CODES | {"REAUTH_REQUIRED"}) else "UPLOAD_FAILED"
    if result.get("status") == "REAUTH_REQUIRED":
        code = "REAUTH_REQUIRED"
    return _mark_failure(job, render, code)


def publish_all_locked(job_id: str) -> dict:
    job = job_store.get(job_id)
    if not job or job.get("id") != job_id:
        raise RenderResolutionError("JOB_NOT_FOUND", 404)
    if not _publishable_job(job):
        raise RenderResolutionError("JOB_NOT_COMPLETED", 409)
    results = []
    rights_code = (_publication_rights_code(job) if any(
        render.get("success") and not (
            render.get("publish_status") == "UPLOADED" and render.get("youtube_video_id"))
        for render in job.get("renders", [])) else None)
    job["bulk_publish_status"] = "RUNNING"
    job["bulk_publish_started_at"] = time.time()
    job_store.set(job_id, job)
    try:
        limit_code = None
        for render in sorted((item for item in job.get("renders", [])
                              if isinstance(item.get("rank"), int)),
                             key=lambda item: item["rank"]):
            rank = render.get("rank")
            if not render.get("success"):
                results.append(_result(rank, "SKIPPED", error_code="RENDER_NOT_COMPLETED"))
                continue
            if limit_code and not (render.get("publish_status") == "UPLOADED" and
                                   render.get("youtube_video_id")):
                results.append(_result(rank, "DEFERRED", error_code=limit_code))
                continue
            result = publish_one_locked(job_id, rank, rights_code)
            results.append(result)
            if result.get("error_code") in PUBLISHING_LIMIT_CODES:
                limit_code = result["error_code"]
    finally:
        current = job_store.get(job_id)
        if current and current.get("bulk_publish_status") == "RUNNING":
            current["bulk_publish_status"] = "IDLE"
            job_store.set(job_id, current)
    # Every successful render was already cleaned inside publish_one_locked.
    # A failed upload retains its final MP4; the completed source is no longer
    # needed when all requested final renders exist.
    try:
        source_cleaned = storage_lifecycle.cleanup_source(job_id)
        if source_cleaned:
            storage_lifecycle.cleanup_job_intermediates(job_id)
    except (OSError, ValueError):
        logger.warning("Could not finish heavy-artifact cleanup after Publish All")
    attempted = [item for item in results if item["status"] in
                 ("UPLOADED", "FAILED", "REAUTH_REQUIRED")]
    success = sum(item["status"] == "UPLOADED" for item in attempted)
    failed = sum(item["status"] in ("FAILED", "REAUTH_REQUIRED") for item in attempted)
    if success and not failed:
        status = "UPLOADED"
    elif success or (failed and any(item["status"] == "ALREADY_UPLOADED" for item in results)):
        status = "PARTIAL_SUCCESS"
    elif failed:
        status = "FAILED"
    else:
        status = "NOTHING_TO_PUBLISH"
    return {"status": status, "results": results}
