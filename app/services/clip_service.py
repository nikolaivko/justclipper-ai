from app.services.job_store import job_store
from typing import Dict, Any, List
from pathlib import Path
from app.models.schemas import ClipCandidate, RenderSettings, EDLSegment
from app.services.ffmpeg_service import FFmpegService
from app.services.transcription_service import TranscriptionService
from app.services.file_service import FileService
from app.services.video_analyzer import LocalVideoAnalyzer
from app.services.youtube_metadata import build_youtube_metadata
from app.services.youtube_source import source_attribution
import hashlib
import json

class ClipService:
    @staticmethod
    def get_job(job_id: str) -> dict:
        return job_store.get(job_id)

    @staticmethod
    def create_job(job_id: str, filename: str, filepath: Path) -> dict:
        job_data = {
            "id": job_id,
            "filename": filename,
            "filepath": str(filepath),
            "status": "IDLE",
            "metadata": None,
            "analysis_data": {},
            "candidates": [],
            "renders": [],
            "progress": None,
            "created_at": __import__('time').time(),
            "lifecycle_version": 1,
            "source_storage_status": "LOCAL",
            "source_cleanup_eligible": False,
        }
        job_store.set(job_id, job_data)
        return job_data

    @staticmethod
    def update_job_status(job_id: str, status: str, error: str = None):
        def mutate(job):
            job["status"] = status
            if status != "FAILED":
                job.pop("error", None)
                job.pop("failed_at", None)
            if status == "FAILED":
                job["failed_at"] = __import__('time').time()
            if status == "COMPLETED":
                job["completed_at"] = __import__('time').time()
            if error:
                job["error"] = error
            from app.services.progress_service import progress_tracker
            if status in {"QUEUED_ANALYSIS", "QUEUED_RENDER"}:
                job["progress"] = None
                return
            prog = progress_tracker.get_job_progress(job_id)
            if prog:
                job["progress"] = prog
        job_store.update(job_id, mutate)

    @staticmethod
    def run_local_analysis(job_id: str, filepath: Path, duration: float | None = None):
        job = job_store.get(job_id)
        if not job: return
        ClipService.update_job_status(job_id, "RUNNING_LOCAL_ANALYSIS")
        
        from app.services.progress_service import progress_tracker
        progress_tracker.update_stage(job_id, "SCENE_DETECTION", "Examining scenes and speech")
        progress_tracker.update_progress(job_id, 0.1, "Detecting silences")
        silences = LocalVideoAnalyzer.detect_silences(filepath)
        
        progress_tracker.update_progress(job_id, 0.5, "Detecting scene cuts")
        scenes = LocalVideoAnalyzer.detect_scenes(filepath)
        
        progress_tracker.update_progress(job_id, 0.8, "Detecting speech")
        
        if duration is None:
            import cv2
            cap = cv2.VideoCapture(str(filepath))
            fps = cap.get(cv2.CAP_PROP_FPS)
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = frames / fps if fps > 0 else 0
            cap.release()
        speech = LocalVideoAnalyzer.speech_from_silences(silences, duration)
        
        job_store.update(job_id, lambda current: current.update(analysis_data={
            "silences": silences, "scenes": scenes, "speech": speech}))

    @staticmethod
    def validate_candidates(candidates: List[ClipCandidate], duration: float,
                            analysis_data: dict = None, events=None,
                            relationships=None, decisions=None) -> List[ClipCandidate]:
        from app.services.editorial_service import prepare_candidates
        return prepare_candidates(candidates, duration, analysis_data, events, relationships,
                                  decisions=decisions)

    @staticmethod
    def process_renders(job_id: str, candidates: List[ClipCandidate], settings: RenderSettings):
        job = job_store.get(job_id)
        if not job:
            return
            
        import logging
        logger = logging.getLogger(__name__)
            
        ClipService.update_job_status(job_id, "RENDERING")
        source_path = Path(job["filepath"])
        renders = []
        disk_full = False
        previous_renders = {render.get("rank"): render for render in job.get("renders", [])}
        
        for i, c in enumerate(candidates):
            prior = previous_renders.get(c.rank)
            if prior and prior.get("publish_status") == "UPLOADED" and prior.get("youtube_video_id"):
                renders.append(prior)
                continue
            signature = hashlib.sha256(json.dumps({
                "candidate": c.model_dump(mode="json"),
                "aspect_mode": settings.aspect_mode,
                "output_resolution": settings.output_resolution,
                "subtitles": settings.subtitles,
                "caption_style_version": "color-accent-v1" if settings.subtitles else None,
            }, sort_keys=True).encode()).hexdigest()
            if (prior and prior.get("success") and
                    prior.get("candidate_signature") == signature and
                    prior.get("storage_status") == "LOCAL" and
                    not (settings.subtitles and
                         prior.get("edit_plan", {}).get("caption_plan", {}).get("status") == "UNAVAILABLE")):
                from app.services.render_resolution import resolve_render, RenderResolutionError
                try:
                    _, _, prior_path = resolve_render(job_id, c.rank)
                    prior_meta = FFmpegService.get_metadata(prior_path)
                    if prior_meta.duration > 0 and prior_meta.width > 0 and prior_meta.height > 0:
                        renders.append(prior)
                        continue
                except (RenderResolutionError, OSError, ValueError, RuntimeError):
                    pass
            out_path = None
            try:
                if job.get("storage_profile") == "DIRECT_UPLOAD":
                    from app.services.storage_lifecycle import storage_lifecycle
                    storage_lifecycle.require_processing_space(job, 64 * 1024 ** 2)
                clip_n = i + 1
                logger.info(f"[PIPELINE] job={job_id} clip={clip_n} stage=VISUAL_ANALYSIS")
                segments_to_track = [(s.start_seconds, s.end_seconds) for s in c.source_segments]
                from app.services.visual_analysis_cache import VisualAnalysisCache
                visual_cache = VisualAnalysisCache()
                signal_key = visual_cache.key(source_path, segments_to_track,
                                              job.get("semantic_cache_key"))
                signals = visual_cache.load(signal_key)
                if signals is None:
                    try:
                        tracks = LocalVideoAnalyzer.track_faces(source_path, segments_to_track)
                    except Exception:
                        logger.warning("Tracking unavailable for job %s; using safe framing", job_id)
                        tracks = []
                    signals = {
                        "tracks": tracks,
                        "scenes": LocalVideoAnalyzer.detect_scenes(source_path,
                                                                    segments=segments_to_track),
                        "speech": LocalVideoAnalyzer.detect_speech(
                            source_path, duration=(job.get("metadata") or {}).get("duration")
                            or FFmpegService.get_metadata(source_path).duration,
                            segments=segments_to_track),
                    }
                    try:
                        visual_cache.save(signal_key, signals)
                    except OSError:
                        logger.warning("Visual signal cache write failed for job %s", job_id)
                face_tracks = signals["tracks"]
                scene_cuts = signals["scenes"]
                speech = signals["speech"]
                
                from app.services.auto_edit_director import AutoEditDirector
                mapped_shots, edit_plan = AutoEditDirector.plan(
                    c, (job.get("metadata") or {}).get("duration") or
                    FFmpegService.get_metadata(source_path).duration, signals)

                # Pass this tracking data down to the render engine
                candidate_dict = c.model_dump()
                candidate_dict["face_tracks"] = face_tracks
                candidate_dict["shot_plan"] = mapped_shots
                
                if settings.subtitles:
                    logger.info(f"[PIPELINE] job={job_id} clip={clip_n} stage=WHISPER")
                    clip_subs = []
                    try:
                        for seg in c.source_segments:
                            seg_subs = TranscriptionService.transcribe_segment(
                                source_path, seg.start_seconds, seg.end_seconds,
                                c.pacing_mode)
                            clip_subs.extend(seg_subs)
                        from app.services.caption_director import CaptionDirector
                        clip_subs, caption_plan = CaptionDirector.plan(c, clip_subs, mapped_shots, face_tracks)
                        edit_plan["caption_plan"] = caption_plan
                        candidate_dict["subtitles"] = clip_subs
                    except Exception as e:
                        logger.error(f"Whisper segment transcription failed: {e}")
                        candidate_dict["subtitles"] = []
                        edit_plan["caption_plan"] = {"status": "UNAVAILABLE", "warning": "CAPTIONS_UNAVAILABLE"}
                else:
                    candidate_dict["subtitles"] = []
                    edit_plan["caption_plan"] = {"status": "DISABLED"}
                
                logger.info(f"[PIPELINE] job={job_id} clip={clip_n} stage=FFMPEG_RENDER")
                out_path = FileService.get_output_path(job["filename"], c.rank, c.title, job_id)
                success = FFmpegService.render_clip(
                    source=source_path,
                    dest=out_path,
                    candidate_data=candidate_dict,
                    aspect_mode=settings.aspect_mode,
                    output_resolution=settings.output_resolution
                )
                
                if success:
                    # VALIDATION
                    logger.info(f"[PIPELINE] job={job_id} clip={clip_n} stage=QC")
                    if not out_path.exists() or out_path.stat().st_size == 0:
                        raise RuntimeError("Render failed: Output file is missing or empty")
                        
                    meta = FFmpegService.get_metadata(out_path)
                    from app.services.render_qc import check_render
                    qc_error = check_render(out_path, meta, mapped_shots,
                                            expect_motion=bool(scene_cuts))
                    if qc_error:
                        raise RuntimeError(f"Render QC: {qc_error}")
                        
                    logger.info(f"[PIPELINE] job={job_id} clip={clip_n} stage=COMPLETE path={out_path}")
                    prior_metadata = prior.get("youtube_metadata") if prior and prior.get("candidate_signature") == signature else None
                    if (source_attribution(job) and prior_metadata and
                            "⚠️ Copyright & Source Notice:" not in
                            prior_metadata.get("description", "")):
                        prior_metadata = None
                    renders.append({
                        "rank": c.rank,
                        "title": c.title,
                        "success": True,
                        "output_path": str(out_path),
                        "storage_status": "LOCAL",
                        "warnings": (["CAPTIONS_UNAVAILABLE"] if edit_plan.get("caption_plan", {}).get("status") == "UNAVAILABLE" else
                                     ["CAPTION_PLACEMENT_REVIEW"] if any(p.get("overlap_risk") for p in edit_plan.get("caption_plan", {}).get("positions", [])) else []),
                        "candidate_signature": signature,
                        "edit_plan": {
                            **edit_plan,
                            "hook_strategy": c.hook_strategy,
                            "pacing_mode": c.pacing_mode,
                            "selected_segments": [s.model_dump() for s in c.source_segments],
                            "removed_segments": [s.model_dump()
                                                 for s in (c.removed_regions or [])],
                            "reaction_segments": [s.model_dump()
                                                  for s in c.reaction_moments],
                            "quality_score": c.editorial_score,
                            "relative_quality": c.relative_quality,
                        },
                        "completed_at": __import__('time').time(),
                        **source_attribution(job),
                        "youtube_metadata": (
                            prior_metadata if prior_metadata
                            else build_youtube_metadata(job, {"rank": c.rank, "title": c.title}, c.model_dump())
                        ),
                    })
                    from app.services.clip_metadata_service import ensure_clip_metadata
                    ensure_clip_metadata(job, renders[-1], out_path, c.model_dump())
                else:
                    raise RuntimeError("FFmpegService returned False")
                    
            except Exception as e:
                from app.services.storage_lifecycle import InsufficientStorage
                if isinstance(e, InsufficientStorage):
                    disk_full = True
                    logger.warning("Render stopped for job %s: insufficient storage", job_id)
                else:
                    logger.exception("Failed to render clip %s for job %s", i + 1, job_id)
                if out_path is not None:
                    from app.services.storage_lifecycle import storage_lifecycle
                    try:
                        storage_lifecycle._delete(out_path, storage_lifecycle.root / "output")
                    except ValueError:
                        pass
                renders.append({
                    "rank": c.rank,
                    "title": c.title,
                    "success": False,
                    "error": "DISK_FULL" if isinstance(e, InsufficientStorage) else "RENDER_FAILED"
                })
                if disk_full:
                    break
            finally:
                # Keep completed outputs recoverable even if a later clip crashes.
                selected = {item["rank"] for item in renders}
                saved = renders + [item for item in previous_renders.values()
                                   if item.get("rank") not in selected]
                job_store.update(job_id, lambda current: current.update(renders=saved))

        selected = {item["rank"] for item in renders}
        renders.extend(item for item in previous_renders.values()
                       if item.get("rank") not in selected and
                       item.get("publish_status") == "UPLOADED")
        job_store.update(job_id, lambda current: current.update(renders=renders))
        
        candidate_ranks = {c.rank for c in candidates}
        successful_renders = sum(1 for r in renders if r.get("success", False)
                                 and r.get("rank") in candidate_ranks)
        if disk_full or successful_renders == 0 or (settings.exact_clip_count and
                                       successful_renders < len(candidates)):
            code = ("DISK_FULL" if disk_full else
                    "INCOMPLETE_RENDER_COUNT" if successful_renders else "RENDER_FAILED")
            logger.error("[PIPELINE] job=%s stage=FAILED success=%s/%s code=%s",
                         job_id, successful_renders, len(candidates), code)
            ClipService.update_job_status(job_id, "FAILED", code)
        else:
            logger.info(f"[PIPELINE] job={job_id} stage=COMPLETED success={successful_renders}/{len(candidates)}")
            ClipService.update_job_status(job_id, "COMPLETED")
            from app.services.storage_lifecycle import storage_lifecycle
            storage_lifecycle.cleanup_source(job_id)
