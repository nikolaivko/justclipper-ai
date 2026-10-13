from contextlib import contextmanager
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import json
import logging
from pathlib import Path
from app.services.file_service import FileService
from app.services.ffmpeg_service import FFmpegService
from app.services.gemini_service import GeminiService
from app.services.clip_service import ClipService
from app.models.schemas import ClipCandidate, RenderSettings, RenderJobRequest
from app.config import config
from app.services.youtube_service import youtube_service
from app.services.download_service import download_service
from app.services.source_rights_service import SourceRightsService
from app.services.heavy_gate import heavy_gate, ProcessingBusy
from app.services.storage_lifecycle import storage_lifecycle, InsufficientStorage, ACTIVE_STATUSES

router = APIRouter()
gemini_service = GeminiService()


class RenderTitleUpdate(BaseModel):
    title: str

@router.get("/diagnostics/storage")
def get_storage_diagnostics():
    from app.services.storage_inventory import storage_inventory
    return storage_inventory()

@router.get("/diagnostics/analysis/{job_id}")
def get_analysis_diagnostics(job_id: str):
    """Count editorial outcomes for a finished analysis without source content."""
    import re
    from collections import Counter
    from app.services.job_store import job_store
    from app.services.semantic_cache import SemanticCache
    from app.services.editorial_service import prepare_candidates

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_store.get(job_id)
    if not job or job.get("status") not in {"FAILED", "READY_TO_RENDER", "COMPLETED"}:
        raise HTTPException(status_code=404, detail="Job not found")
    key = job.get("semantic_cache_key")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        return {"cache_available": False}
    cached = SemanticCache().load(key)
    if cached is None:
        return {"cache_available": False}
    analysis, local_analysis, _ = cached
    outcomes = []
    selected = prepare_candidates(
        analysis.clips, (job.get("metadata") or {}).get("duration") or 0,
        local_analysis, analysis.events, analysis.narrative_relationships,
        audit=outcomes)
    return {"cache_available": True, "raw_candidates": len(analysis.clips),
            "selected_candidates": len(selected),
            "outcomes": dict(sorted(Counter(outcomes).items()))}

@router.get("/diagnostics")
def get_diagnostics():
    ffmpeg_ok = FFmpegService.check_installed()
    gemini_setup = gemini_service.check_setup()
    return {
        "ffmpeg_installed": ffmpeg_ok,
        "gemini_configured": gemini_setup["configured"],
        "gemini_model": gemini_setup["model"]
    }

@router.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    job_id = FileService.get_unique_job_id()
    try:
        heavy_gate.claim(job_id)
    except ProcessingBusy:
        raise HTTPException(status_code=409, detail="PROCESSING_BUSY")
    try:
        try:
            storage_lifecycle.prepare_for_new_video()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Upload preflight cleanup failed: type=%s", type(exc).__name__)
        storage_lifecycle.require_upload_space(0)
        dest_path = await FileService.save_upload(file, job_id)
        try:
            metadata = FFmpegService.get_metadata(dest_path)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="INVALID_VIDEO_FILE")
        try:
            job = ClipService.create_job(job_id, file.filename, dest_path)
            job["storage_profile"] = "DIRECT_UPLOAD"
            job["source_cleanup_eligible"] = True
            job["metadata"] = metadata.model_dump()
            from app.services.job_store import job_store as js
            js.set(job_id, job)
            return {"job_id": job_id, "metadata": metadata.model_dump()}
        except Exception:
            dest_path.unlink(missing_ok=True)
            ClipService.update_job_status(job_id, "FAILED", "REGISTRATION_FAILED")
            raise
    except InsufficientStorage:
        raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE")
    finally:
        heavy_gate.release(job_id)


@router.post("/upload/stream")
async def upload_video_stream(request: Request):
    """Browser upload path: receive one raw file without multipart spooling."""
    from urllib.parse import unquote
    import logging

    raw_name = request.headers.get("x-upload-filename", "")
    raw_size = request.headers.get("x-upload-size", "")
    try:
        size = int(raw_size)
    except ValueError:
        size = 0
    if not raw_name or len(raw_name) > 768 or size <= 0:
        raise HTTPException(status_code=400, detail="INVALID_UPLOAD")
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length != str(size):
        raise HTTPException(status_code=400, detail="UPLOAD_SIZE_MISMATCH")
    filename = unquote(raw_name)
    job_id = FileService.get_unique_job_id()
    try:
        heavy_gate.claim(job_id)
    except ProcessingBusy:
        raise HTTPException(status_code=409, detail="PROCESSING_BUSY")
    try:
        try:
            storage_lifecycle.prepare_for_new_video()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Upload preflight cleanup failed: type=%s", type(exc).__name__)
        storage_lifecycle.require_upload_space(size)
        dest_path = await FileService.save_stream(request.stream(), job_id, filename, size)
        try:
            metadata = FFmpegService.get_metadata(dest_path)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="INVALID_VIDEO_FILE")
        try:
            job = ClipService.create_job(job_id, filename, dest_path)
            job["storage_profile"] = "DIRECT_UPLOAD"
            job["source_cleanup_eligible"] = True
            job["metadata"] = metadata.model_dump()
            from app.services.job_store import job_store as js
            js.set(job_id, job)
            return {"job_id": job_id, "metadata": metadata.model_dump()}
        except Exception:
            dest_path.unlink(missing_ok=True)
            ClipService.update_job_status(job_id, "FAILED", "REGISTRATION_FAILED")
            raise
    except InsufficientStorage:
        raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE")
    except ValueError:
        raise HTTPException(status_code=400, detail="INVALID_UPLOAD")
    except OSError as exc:
        import errno
        if exc.errno == errno.ENOSPC:
            raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE") from exc
        raise
    finally:
        heavy_gate.release(job_id)

@contextmanager
def pipeline_admission(job_id: str):
    # Publishing and metadata editing must not race with a new pipeline run.
    from app.services.youtube_publish_service import publishing_lock
    from app.services.render_resolution import RenderResolutionError
    try:
        with publishing_lock(job_id, blocking=False):
            yield
    except RenderResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)


@router.post("/analyze/{job_id}")
async def analyze_video(job_id: str, background_tasks: BackgroundTasks, settings: RenderSettings):
    if settings.auto_edit:
        from app.services.auto_edit_director import AutoEditDirector
        try:
            settings = AutoEditDirector.settings(settings.editorial_guidance, settings.target_clip_count, settings.count_mode)
        except ValueError:
            raise HTTPException(status_code=422, detail="INVALID_CLIP_COUNT")
    with pipeline_admission(job_id):
        job = ClipService.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        duration = (job.get("metadata") or {}).get("duration") or 0
        if (settings.clip_count < 1 or settings.clip_count > 30 or
                settings.min_clip_seconds < 5 or
                settings.max_clip_seconds < settings.min_clip_seconds):
            raise HTTPException(status_code=422, detail="INVALID_ANALYSIS_SETTINGS")
        if settings.exact_clip_count and duration < settings.clip_count * settings.min_clip_seconds:
            raise HTTPException(status_code=422, detail="SOURCE_TOO_SHORT_FOR_TARGET")
        if job.get("status") in ACTIVE_STATUSES:
            import logging
            logging.getLogger(__name__).info(f"DUPLICATE_ENQUEUE_PREVENTED job_id={job_id} status={job.get('status')}")
            return {"status": "Processing already queued"}
        try:
            storage_lifecycle.require_processing_space(job)
            heavy_gate.claim(job_id)
        except InsufficientStorage:
            raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE")
        except ProcessingBusy:
            raise HTTPException(status_code=409, detail="PROCESSING_BUSY")
        if not job.get("filepath") or not Path(job["filepath"]).is_file():
            heavy_gate.release(job_id)
            raise HTTPException(status_code=409, detail="SOURCE_FILE_MISSING")
        
        try:
            job["analysis_settings"] = settings.model_dump()
            job["auto_render"] = settings.auto_edit
            from app.services.job_store import job_store as js
            js.set(job_id, job)
            ClipService.update_job_status(job_id, "QUEUED_ANALYSIS")
            
            # Count queued jobs for observability
            queued = [j for j in js.get_all() if j.get("status") in ["QUEUED_ANALYSIS", "QUEUED_RENDER"]]
            import logging
            logging.getLogger(__name__).info(f"JOB_ENQUEUED job_id={job_id} queue_position={len(queued)}")
        except Exception as e:
            heavy_gate.release(job_id)
            import logging
            logging.getLogger(__name__).error(f"Failed to enqueue job {job_id}: {e}")
            raise
        return {"status": "Analysis queued"}

class AutoEditRequest(BaseModel):
    editorial_guidance: str | None = None
    clip_count: int | None = None
    target_clip_count: int | None = None
    count_mode: str = "AUTO"


@router.post("/auto-edit/{job_id}")
async def auto_edit_video(job_id: str, background_tasks: BackgroundTasks,
                          request: AutoEditRequest = AutoEditRequest()):
    from app.services.auto_edit_director import AutoEditDirector
    try:
        settings = AutoEditDirector.settings(request.editorial_guidance, request.target_clip_count or request.clip_count, request.count_mode)
    except ValueError:
        raise HTTPException(status_code=422, detail="INVALID_CLIP_COUNT")
    return await analyze_video(job_id, background_tasks, settings)


@router.get("/status/{job_id}")
def get_status(job_id: str):
    import re
    job = ClipService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Stage callbacks update memory continuously; persisted progress is a restart fallback.
    from app.services.progress_service import progress_tracker
    live_progress = progress_tracker.get_job_progress(job_id)
    if live_progress and job.get("status") not in {"IDLE", "QUEUED_ANALYSIS", "QUEUED_RENDER"}:
        job["progress"] = live_progress
        
    if job.get("status") in ["QUEUED_ANALYSIS", "QUEUED_RENDER"]:
        from app.services.job_store import job_store
        all_jobs = job_store.get_all()
        active = [j for j in all_jobs if j.get("status") not in ["IDLE", "QUEUED_ANALYSIS", "QUEUED_RENDER", "COMPLETED", "FAILED", "READY_TO_RENDER"]]
        
        queued = [j for j in all_jobs if j.get("status") in ["QUEUED_ANALYSIS", "QUEUED_RENDER"]]
        queued.sort(key=lambda j: j.get("created_at", 0))
        
        pos = 0
        if len(active) > 0:
            pos += 1
            
        for qj in queued:
            if qj["id"] == job_id:
                break
            pos += 1
            
        job["queue_position"] = pos + 1 if len(active) == 0 else pos
        
    public_job = {k: v for k, v in job.items()
                  if k not in {"filepath", "semantic_cache_key"}}
    if public_job.get("status") != "FAILED":
        public_job.pop("error", None)
        public_job.pop("failed_at", None)
    source_id = job.get("source_id") or (
        job_id[4:] if job_id.startswith("job_youtube_") else None)
    if source_id and re.fullmatch(r"youtube_[A-Za-z0-9_-]{11}", source_id):
        from app.services.source_store import source_store
        asset = source_store.get(source_id)
        public_job["rights_status"] = asset.rights_status if asset else "UNKNOWN"
        public_job["rights_evidence"] = asset.rights_evidence if asset else None
        public_job["source_visibility"] = asset.source_visibility if asset else None
    if public_job.get("error") and not re.fullmatch(r"[A-Z_0-9]+", str(public_job["error"])):
        public_job["error"] = "PROCESSING_FAILED"
    public_job["renders"] = [
        {k: ("RENDER_FAILED" if k == "error" and not re.fullmatch(r"[A-Z_0-9]+", str(v)) else v)
         for k, v in render.items() if k != "output_path"}
        for render in job.get("renders", [])
    ]
    return public_job

@router.get("/edit-plan/{job_id}/{rank}")
def get_edit_plan(job_id: str, rank: int):
    """Compact development view of the persisted editorial decisions."""
    job = ClipService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    render = next((item for item in job.get("renders", [])
                   if item.get("rank") == rank), None)
    candidate = next((item for item in job.get("candidates", [])
                      if item.get("rank") == rank), None)
    if not candidate:
        raise HTTPException(status_code=404, detail="CANDIDATE_NOT_FOUND")
    plan = (render or {}).get("edit_plan", {})
    return {
        "rank": rank,
        "style": plan.get("style_profile", candidate.get("style_profile")),
        "hook": plan.get("hook_strategy", candidate.get("hook_strategy")),
        "pacing": plan.get("pacing_mode", candidate.get("pacing_mode")),
        "source": plan.get("selected_segments", candidate.get("source_segments", [])),
        "removed": plan.get("removed_segments", candidate.get("removed_regions", [])),
        "payoff": candidate.get("payoff_timestamp"),
        "reaction": plan.get("reaction_segments", candidate.get("reaction_moments", [])),
        "effects": plan.get("effect_events", []),
        "effect_budget": plan.get("effect_budget"),
        "overediting_score": plan.get("overediting_score"),
        "quality_score": plan.get("quality_score", candidate.get("editorial_score")),
        "critic_issue": candidate.get("critic_issue"),
    }


@router.patch("/jobs/{job_id}/renders/{rank}/title")
def update_render_title(job_id: str, rank: int, request: RenderTitleUpdate):
    """Save a reviewed title for a rendered clip before uploading it."""
    import re
    from app.services.job_store import job_store
    from app.services.youtube_publish_service import publishing_lock
    from app.services.youtube_metadata import build_youtube_metadata, is_placeholder_title

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    title = " ".join(request.title.split())
    if is_placeholder_title(title) or len(title) > 100 or any(char in title for char in "<>"):
        raise HTTPException(status_code=422, detail="INVALID_TITLE")
    with publishing_lock(job_id):
        job = job_store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
        render = next((r for r in job.get("renders", []) if r.get("rank") == rank), None)
        if job.get("status") in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="JOB_BUSY")
        if not render or not render.get("success"):
            raise HTTPException(status_code=404, detail="RENDER_NOT_FOUND")
        if render.get("publish_status") in {"UPLOADING", "UPLOADED"}:
            raise HTTPException(status_code=409, detail="ALREADY_PUBLISHING")
        metadata = render.get("youtube_metadata") or build_youtube_metadata(job, render)
        if is_placeholder_title(metadata.get("title")):
            metadata = build_youtube_metadata(job, render, {
                "rank": rank, "title": title,
                "short_description": render.get("generated_description") or ""})
        metadata["title"] = title
        render["youtube_metadata"] = metadata
        render["metadata_status"] = "REVIEWED"
        render.pop("metadata_error", None)
        job_store.set(job_id, job)
    return {"rank": rank, "title": title}

@router.post("/jobs/{job_id}/renders/{rank}/metadata/generate")
def generate_render_metadata(job_id: str, rank: int):
    from app.services.job_store import job_store
    from app.services.render_resolution import resolve_render, RenderResolutionError
    from app.services.youtube_publish_service import publishing_lock, _publishable_job
    from app.services.clip_metadata_service import ensure_clip_metadata

    try:
        with publishing_lock(job_id, blocking=False):
            job, render, path = resolve_render(job_id, rank)
            if not _publishable_job(job):
                raise HTTPException(status_code=409, detail="JOB_BUSY")
            if render.get("publish_status") in {"UPLOADING", "UPLOADED"}:
                raise HTTPException(status_code=409, detail="ALREADY_PUBLISHING")
            try:
                heavy_gate.claim(job_id)
            except ProcessingBusy:
                raise HTTPException(status_code=409, detail="PROCESSING_BUSY")
            try:
                ensure_clip_metadata(job, render, path, force=True)
                job_store.set(job_id, job)
            finally:
                heavy_gate.release(job_id)
            return {"rank": rank, "metadata_status": render.get("metadata_status"),
                    "youtube_metadata": render["youtube_metadata"],
                    "metadata_error": render.get("metadata_error")}
    except RenderResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)


@router.post("/render/{job_id}")
async def render_clips(job_id: str, request: RenderJobRequest, background_tasks: BackgroundTasks):
    with pipeline_admission(job_id):
        job = ClipService.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") in ACTIVE_STATUSES:
            import logging
            logging.getLogger(__name__).info(f"DUPLICATE_ENQUEUE_PREVENTED job_id={job_id} status={job.get('status')}")
            return {"status": "Processing already queued"}
        try:
            storage_lifecycle.require_processing_space(job)
            heavy_gate.claim(job_id)
        except InsufficientStorage:
            raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE")
        except ProcessingBusy:
            raise HTTPException(status_code=409, detail="PROCESSING_BUSY")
        try:
            if not job.get("filepath") or not Path(job["filepath"]).is_file():
                raise HTTPException(status_code=409, detail="SOURCE_FILE_MISSING")
        
            req_dict = request.model_dump()
            if not req_dict.get("candidates"):
                # A retry must keep the reviewed edits from the previous render request.
                req_dict["candidates"] = (
                    (job.get("render_request") or {}).get("candidates")
                    or job.get("candidates") or []
                )
            if not req_dict["candidates"]:
                raise HTTPException(status_code=400, detail="NO_CLIP_CANDIDATES")
            requested = (job.get("analysis_settings") or {}).get("clip_count")
            exact = (job.get("analysis_settings") or {}).get("exact_clip_count") is True
            req_dict["settings"]["clip_count"] = len(req_dict.get("candidates", []))
            req_dict["settings"]["exact_clip_count"] = bool(
                exact and requested == len(req_dict.get("candidates", [])))
            ranks = [candidate.get("rank") for candidate in req_dict["candidates"]]
            if len(ranks) != len(set(ranks)):
                raise HTTPException(status_code=400, detail="DUPLICATE_CLIP_RANK")
            published_ranks = {r.get("rank") for r in job.get("renders", [])
                               if r.get("publish_status") == "UPLOADED"}
            reviewed = {c.get("rank"): c for c in
                        (job.get("render_request") or {}).get("candidates", [])}
            for candidate in req_dict["candidates"]:
                pass # Edit lock removed per user request
            from app.services.editorial_service import validate_final_edl
            source_duration = (job.get("metadata") or {}).get("duration", 0)
            originals = {candidate.get("rank"): candidate
                         for candidate in job.get("candidates", [])}
            for raw in req_dict.get("candidates", []):
                stored_overlay = (originals.get(raw.get("rank")) or {}).get("context_overlay")
                if raw.get("context_overlay") != stored_overlay:
                    raw["context_overlay"] = None
                error = validate_final_edl(ClipCandidate.model_validate(raw), source_duration)
                if error:
                    raise HTTPException(status_code=400, detail=error)
        
            job["render_request"] = req_dict
            from app.services.job_store import job_store as js
            js.set(job_id, job)
            ClipService.update_job_status(job_id, "QUEUED_RENDER")
        except ValueError as exc:
            heavy_gate.release(job_id)
            raise HTTPException(status_code=422, detail="INVALID_RENDER_REQUEST") from exc
        except Exception:
            heavy_gate.release(job_id)
            raise
        return {"status": "Rendering queued"}

@router.get("/jobs/{job_id}/renders/{rank}/media")
def get_job_media(job_id: str, rank: int):
    from app.services.render_resolution import resolve_render, RenderResolutionError
    try:
        _, _, filepath = resolve_render(job_id, rank)
    except RenderResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return FileResponse(filepath, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})



@router.get("/trends")
def get_trends(page_token: str = None, force_refresh: bool = False):
    if not youtube_service.is_configured():
        return {"configured": False, "error": "YouTube Data API Key is not configured"}
    try:
        trends, next_token = youtube_service.get_us_trends(
            max_results=50, force_refresh=force_refresh, page_token=page_token)
        decorated = []
        for trend in trends:
            technical = download_service.technical_downloadability(trend["id"])
            if trend.get("privacy_status") != "public" or trend.get("upload_status") != "processed":
                technical = "BLOCKED"
            decorated.append({**trend,
                              "acquisition_status": download_service.acquisition_state(trend["id"]),
                              "acquisition_error_code": download_service.acquisition_diagnostic(trend["id"]),
                              "technical_downloadability": technical})
        return {"configured": True, "trends": decorated, "next_page_token": next_token,
                "feed_stats": getattr(youtube_service, "last_diagnostics", {})}
    except ValueError as e:
        return {"configured": True, "error": str(e)}
    except Exception as e:
        return {"configured": True, "error": str(e)}

@router.post("/download/youtube")
async def download_youtube(request: Request):
    import re
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="INVALID_YOUTUBE_REQUEST")
    video_id = data.get("video_id")
    if "url" in data:
        from app.services.youtube_source import parse_youtube_url
        try:
            parsed_id = parse_youtube_url(data["url"])
            if video_id and parsed_id != video_id:
                raise ValueError()
            video_id = parsed_id
        except ValueError:
            raise HTTPException(status_code=400, detail="INVALID_YOUTUBE_URL")
    auto_options = {}
    if data.get("auto_edit") is True:
        from app.services.auto_edit_director import AutoEditDirector
        try:
            options = AutoEditDirector.settings(None, data.get("target_clip_count", data.get("clip_count")), data.get("count_mode", "AUTO"))
            auto_options["auto_edit_settings"] = options.model_dump()
        except ValueError:
            raise HTTPException(status_code=422, detail="INVALID_CLIP_COUNT")
    title = data.get("title", "")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="INVALID_VIDEO_ID")
    if not isinstance(title, str):
        title = ""
    try:
        verified = youtube_service.get_video_rights(video_id)
    except Exception:
        raise HTTPException(status_code=503, detail="VERIFICATION_UNAVAILABLE")
    if not verified:
        raise HTTPException(status_code=404, detail="VIDEO_UNAVAILABLE")
    if verified.get("rights_status") == "RESTRICTED":
        raise HTTPException(status_code=403, detail="RIGHTS_BLOCKED")
    if verified.get("privacy_status") != "public" or verified.get("upload_status") != "processed":
        raise HTTPException(status_code=403, detail="VIDEO_UNAVAILABLE")
    if not SourceRightsService.allows_public_acquisition(
            verified.get("rights_status"), public_verified=True):
        code = "RIGHTS_BLOCKED" if verified.get("rights_status") == "RESTRICTED" else "RIGHTS_UNVERIFIED"
        raise HTTPException(status_code=403, detail=code)
    from app.services.youtube_acquisition import RETRYABLE_ACQUISITION_ERRORS
    if (download_service.technical_downloadability(video_id) == "BLOCKED" and
            download_service.acquisition_diagnostic(video_id) not in RETRYABLE_ACQUISITION_ERRORS):
        raise HTTPException(status_code=409, detail="ACQUISITION_BLOCKED")
    
    try:
        dl_id = download_service.enqueue_download(
            video_id, verified.get("title") or title,
            rights_status=verified["rights_status"],
            rights_evidence=verified.get("rights_evidence", ""),
            source_video_title=verified.get("title") or "",
            source_channel_name=verified.get("channel", ""),
            source_channel_id=verified.get("channel_id", ""),
            public_verified=True, force_retry=True, **auto_options)
    except ValueError:
        raise HTTPException(status_code=400, detail="INVALID_VIDEO_ID")
    except ProcessingBusy:
        raise HTTPException(status_code=409, detail="PROCESSING_BUSY")
    except InsufficientStorage:
        raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE")
    return {"dl_id": dl_id}

@router.get("/download/{dl_id}")
def get_download_status(dl_id: str):
    status = download_service.get_status(dl_id)
    if not status:
        raise HTTPException(status_code=404, detail="Download not found")
    return status


@router.get("/sources")
def get_sources():
    from app.services.source_store import source_store
    from app.services.job_store import job_store
    from app.services.storage_lifecycle import ACTIVE_STATUSES
    sources = source_store.get_all()
    entries = []
    for source in sources:
        if source.storage_status == "CLEANED" or not source.source_path or not Path(source.source_path).is_file():
            continue
        entry = {k: v for k, v in source.model_dump().items() if k != "source_path"}
        job = job_store.get(f"job_{source.source_id}")
        status = (job or {}).get("status")
        if status in ACTIVE_STATUSES:
            entry["library_status"] = "PROCESSING"
        elif status == "FAILED":
            entry["library_status"] = "FAILED"
        elif status == "COMPLETED" and job.get("renders") and all(
                render.get("publish_status") == "UPLOADED" for render in job["renders"]):
            entry["library_status"] = "PUBLISHED"
        elif status == "COMPLETED":
            entry["library_status"] = "READY"
        else:
            entry["library_status"] = "ACTIVE"
        entries.append(entry)
    return {"sources": entries}

@router.get("/sources/{source_id}/media")
def get_source_media(source_id: str, request: Request):
    from app.services.source_store import source_store
    source = source_store.get(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
        
    if source.storage_status == "CLEANED" or not source.source_path:
        raise HTTPException(status_code=404, detail="SOURCE_FILE_MISSING")
    filepath = Path(source.source_path)
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Media file not found")
        
    # We must restrict to DATA_ROOT
    try:
        filepath.resolve().relative_to(config.DATA_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden path")

    from fastapi.responses import FileResponse
    return FileResponse(filepath, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})


@router.post("/sources/{source_id}/process")
def process_source(source_id: str):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
        raise HTTPException(status_code=400, detail="INVALID_SOURCE_ID")
    from app.services.source_store import source_store
    source = source_store.get(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.provider == "youtube" and not SourceRightsService.allows_public_acquisition(
            source.rights_status, public_verified=source.source_visibility == "public") and not (
            source.source_visibility is None and SourceRightsService.allows_auto_acquisition(source.rights_status)):
        code = "RIGHTS_BLOCKED" if source.rights_status == "RESTRICTED" else "RIGHTS_UNVERIFIED"
        raise HTTPException(status_code=403, detail=code)
    if source.storage_status == "CLEANED" or not source.source_path or not Path(source.source_path).is_file():
        raise HTTPException(status_code=409, detail="SOURCE_FILE_MISSING")
        
    job_id = f"job_{source_id}"
    with pipeline_admission(job_id):
        job = ClipService.get_job(job_id)
        if job and any(r.get("publish_status") in {"UPLOADING", "UPLOADED"} for r in job.get("renders", [])):
            return {"status": "Existing published job", "job_id": job_id}
        if job and job.get("status") in ACTIVE_STATUSES:
            return {"status": "Processing already queued", "job_id": job_id}
        try:
            storage_lifecycle.require_space()
            heavy_gate.claim(job_id)
        except InsufficientStorage:
            raise HTTPException(status_code=507, detail="INSUFFICIENT_STORAGE")
        except ProcessingBusy:
            raise HTTPException(status_code=409, detail="PROCESSING_BUSY")

        try:
            job = ClipService.get_job(job_id)
            if not job:
                filepath = Path(source.source_path)
                job = ClipService.create_job(job_id, f"{source.title}.mp4", filepath)
                job["source_id"] = source_id
                job["source_cleanup_eligible"] = source.storage_lifecycle_version == 1
                metadata = FFmpegService.get_metadata(filepath)
                job["metadata"] = metadata.model_dump()
            from app.services.auto_edit_director import AutoEditDirector
            job["analysis_settings"] = AutoEditDirector.settings().model_dump()
            job["auto_render"] = True
            from app.services.job_store import job_store as js
            js.set(job_id, job)
            ClipService.update_job_status(job_id, "QUEUED_ANALYSIS")
            return {"status": "Processing queued", "job_id": job_id}
        except Exception:
            heavy_gate.release(job_id)
            raise

@router.get("/downloads/pending")
def get_pending_downloads():
    from app.services.download_service import download_service
    with download_service.lock:
        pending = []
        for dl_id, info in download_service.active_downloads.items():
            if info["status"] in ["QUEUED", "DOWNLOADING", "FAILED", "AUTH_REQUIRED",
                                  "ACQUISITION_BLOCKED", "RIGHTS_BLOCKED", "UNAVAILABLE"]:
                pending.append({
                    "id": dl_id,
                    "video_id": info["video_id"],
                    "title": info["title"]
                })
        return pending
