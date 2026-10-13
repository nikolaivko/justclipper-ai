import time
import threading
import logging
from pathlib import Path
from app.services.job_store import job_store
from app.services.clip_service import ClipService
from app.services.progress_service import progress_tracker
from app.config import config
from app.services.heavy_gate import heavy_gate, ProcessingBusy
from app.services.storage_lifecycle import storage_lifecycle, InsufficientStorage

logger = logging.getLogger(__name__)

class PipelineWorker:
    def __init__(self):
        self.running = False
        self.thread = None
        self._last_cleanup = time.time()

    def start(self):
        if not self.running:
            self._recover_jobs()
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            logger.info("Pipeline worker started.")

    def _recover_jobs(self):
        jobs = job_store.get_all()
        for job in jobs:
            status = job.get("status")
            if status in ["PREPARING_ANALYSIS_MEDIA", "GENERATING_PROXY", "UPLOADING_TO_GEMINI", "WAITING_FOR_GEMINI", "VALIDATING_CANDIDATES", "RUNNING_LOCAL_ANALYSIS"]:
                logger.info(f"Recovering interrupted analysis job {job['id']}")
                if job.get("lifecycle_version") == 1:
                    storage_lifecycle.cleanup_job_intermediates(job["id"])
                ClipService.update_job_status(job['id'], "QUEUED_ANALYSIS")
            elif status in ["RENDERING"]:
                logger.info(f"Recovering interrupted render job {job['id']}")
                if job.get("lifecycle_version") == 1:
                    storage_lifecycle.cleanup_job_intermediates(job["id"])
                ClipService.update_job_status(job['id'], "QUEUED_RENDER")


    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5.0)

    def _loop(self):

        while self.running:
            # Simple FIFO queue logic: grab all jobs, pick one that needs work
            # For Railway, MAX_HEAVY_JOBS=1 is achieved by this single threaded loop!
            try:
                jobs = job_store.get_all()
                jobs.sort(key=lambda j: j.get("created_at", 0)) # simple ordering

                # Check for interrupted jobs first
                job_to_process = None
                for job in jobs:
                    status = job.get("status")
                    if status in ["QUEUED_ANALYSIS", "QUEUED_RENDER"]:
                        job_to_process = job
                        break

                if job_to_process:
                    self._process_job(job_to_process)
                else:
                    time.sleep(2)
                if time.time() - self._last_cleanup >= 3600:
                    self._last_cleanup = time.time()
                    storage_lifecycle.cleanup_failed_jobs()
                    storage_lifecycle.cleanup_expired_outputs()
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                time.sleep(5)

    def _process_job(self, job: dict):
        job_id = job["id"]
        status = job["status"]
        logger.info(f"JOB_CLAIMED job_id={job_id} status={status}")
        try:
            heavy_gate.claim(job_id)
            logger.info(f"HEAVY_SLOT_ACQUIRED job_id={job_id}")
        except ProcessingBusy:
            time.sleep(2)
            return
        try:
            storage_lifecycle.require_processing_space(job)
            if status == "QUEUED_ANALYSIS":
                logger.info(f"ANALYSIS_STARTED job_id={job_id}")
                self._process_analysis(job)
                updated = job_store.get(job_id)
                if updated and updated.get("status") == "QUEUED_RENDER":
                    self._process_render(updated)
            elif status == "QUEUED_RENDER":
                logger.info(f"RENDER_STARTED job_id={job_id}")
                self._process_render(job)
            final = job_store.get(job_id) or {}
            logger.info("JOB_FINISHED job_id=%s status=%s", job_id, final.get("status"))
        except InsufficientStorage:
            logger.warning("Insufficient storage before job %s", job_id)
            logger.error(f"JOB_FAILED job_id={job_id} stage=PREPARATION error_category=INSUFFICIENT_STORAGE")
            progress_tracker.fail_job(job_id, "INSUFFICIENT_STORAGE")
            ClipService.update_job_status(job_id, "FAILED", "INSUFFICIENT_STORAGE")
        except Exception:
            logger.exception("Job failed before completing its pipeline: job_id=%s", job_id)
            progress_tracker.fail_job(job_id, "PROCESSING_FAILED")
            ClipService.update_job_status(job_id, "FAILED", "PROCESSING_FAILED")
        finally:
            from app.services.transcription_service import TranscriptionService
            TranscriptionService.release_model()
            heavy_gate.release(job_id)
            logger.info(f"HEAVY_SLOT_RELEASED job_id={job_id}")

    def _process_analysis(self, job: dict):
        job_id = job["id"]
        try:
            self._run_analysis(job)
        except Exception as e:
            logger.error("Analysis failed for job %s: type=%s", job_id, type(e).__name__)
            reason = str(e)
            from app.services.gemini_requests import transient_failure
            safe_reason = reason if reason in {
                "NO_SUITABLE_CLIPS", "ALL_CANDIDATES_REJECTED",
                "EXACT_COUNT_UNAVAILABLE", "INSUFFICIENT_STORAGE",
                "ANALYSIS_PROXY_FAILED",
                "AI_INVALID_TIMELINE", "AI_QUOTA_EXHAUSTED",
                "AI_SERVICE_UNAVAILABLE", "AI_COMPRESSION_SERVICE_UNAVAILABLE",
                "AI analysis is temporarily unavailable due to high demand. Please try again in a few minutes.",
                "AI analysis failed because all configured Gemini models have no available API quota.",
                "AI analysis failed because the request is incompatible with the available models.",
                "AI analysis failed after trying all configured models.",
            } else "ANALYSIS_FAILED"
            if getattr(e, "error_code", None) == "AI_QUOTA_EXHAUSTED":
                safe_reason = "AI_QUOTA_EXHAUSTED"
            elif transient_failure(e):
                safe_reason = "AI_SERVICE_UNAVAILABLE"
            if safe_reason == "ANALYSIS_FAILED":
                from app.services.clip_metadata_service import title_error_code
                provider_code = title_error_code(e.__cause__ or e)
                if provider_code.startswith("GEMINI_"):
                    safe_reason = provider_code
            progress_tracker.fail_job(job_id, safe_reason)
            job_store.update(job_id, lambda current: current.update(
                analysis_retryable=safe_reason in {"AI_QUOTA_EXHAUSTED", "AI_SERVICE_UNAVAILABLE", "AI_COMPRESSION_SERVICE_UNAVAILABLE"}))
            ClipService.update_job_status(job_id, "FAILED", safe_reason)
        finally:
            current = job_store.get(job_id) or job
            if (job.get("lifecycle_version") == 1 and not current.get("analysis_retryable")
                    and not current.get("preserve_analysis_artifacts")):
                storage_lifecycle.cleanup_job_intermediates(job_id)

    def _process_render(self, job: dict):
        job_id = job["id"]
        try:
            self._run_render(job)
        except Exception as e:
            logger.exception("Render failed for job %s", job_id)
            progress_tracker.fail_job(job_id, "RENDER_FAILED")
            ClipService.update_job_status(job_id, "FAILED", "RENDER_FAILED")
        finally:
            if job.get("lifecycle_version") == 1 and not job.get("preserve_analysis_artifacts"):
                storage_lifecycle.cleanup_job_intermediates(job_id)

    def _run_analysis(self, job: dict):
        job_id = job["id"]
        import json
        from app.services.gemini_service import GeminiService, PROMPT_VERSION
        from app.services.semantic_cache import SemanticCache
        from app.services.ffmpeg_service import FFmpegService
        from app.services.file_service import FileService
        gemini_service = GeminiService()
        def on_stage(stage, message):
            progress_tracker.update_stage(job_id, stage, message)
            status = {"UPLOADING_TO_AI": "UPLOADING_TO_GEMINI",
                      "WAITING_FOR_AI_FILE": "WAITING_FOR_GEMINI",
                      "ANALYZING_WITH_AI": "WAITING_FOR_GEMINI",
                      "BUILDING_CANDIDATES": "WAITING_FOR_GEMINI"}.get(stage, stage)
            ClipService.update_job_status(job_id, status)
        gemini_service.on_stage = on_stage
        def on_request_event(event):
            job_store.update(job_id, lambda current: current.setdefault("ai_request_history", []).append(event))
            if event.get("category") == "REQUEST_STARTED":
                progress_tracker.update_progress(job_id, 0,
                    f"AI: {event['phase']} · {event['model']} · attempt {event['attempt']} (up to 3 min)")
            elif event.get("category") == "SERVICE_UNAVAILABLE":
                progress_tracker.update_progress(job_id, 0, "AI did not respond; trying another available model")
        gemini_service.on_request_event = on_request_event
        gemini_service.on_semantic_result = lambda phase, result: job_store.update(
            job_id, lambda current: current.setdefault("semantic_passes", []).append(
                {"phase": phase, "result": result}))

        if not job.get("metadata"):
            job["metadata"] = FFmpegService.get_metadata(Path(job["filepath"])).model_dump()
            job_store.set(job_id, job)

        progress_tracker.start_job(job_id, job["metadata"]["duration"], "ANALYSIS")
        progress_tracker.update_stage(job_id, "PREPARING_VIDEO", "Preparing media for analysis")
        ClipService.update_job_status(job_id, "PREPARING_ANALYSIS_MEDIA")

        source_path = Path(job["filepath"])
        proxy_path = FileService.get_proxy_path(source_path, job_id)
        settings_dump = dict(job.get("analysis_settings", {}))
        cache = SemanticCache()
        cache_key = cache.key(source_path, settings_dump)
        job["semantic_cache_key"] = cache_key
        job_store.set(job_id, job)
        cached = cache.load(cache_key)
        reusable_local = (job.get("analysis_data")
                          if job.get("checkpoint_cache_key") == cache_key else None)
        from app.services.semantic_validation import timeline_audit, InvalidSemanticTimeline
        if cached and any(not item["valid"] for item in
                          timeline_audit(cached[0], job["metadata"]["duration"])):
            # Preserve the failed cache as evidence; it is bound to this exact
            # source hash and settings, so its local scene/silence map is reusable.
            reusable_local = cached[1]
            job["invalid_cached_timeline"] = timeline_audit(cached[0], job["metadata"]["duration"])
            cache_key += "-timeline-v1"
            job["semantic_cache_key"] = cache_key
            job_store.set(job_id, job)
            cached = cache.load(cache_key)
        if cached and cached[0].source_summary.startswith("AI analysis was unavailable."):
            # Never pin a temporary provider outage in the semantic cache.
            cached = None
        if cached and not cached[0].clips and not job.get("empty_cache_recovery_attempted"):
            # Recover a failed job created before the focused Gemini pass existed.
            # Mark first so the same empty cache cannot trigger unlimited reanalysis.
            job["empty_cache_recovery_attempted"] = True
            job_store.set(job_id, job)
            cached = None
        if cached:
            stats = cached[2]
            discovery_capacity = stats.get("discovery_capacity", 0)
            actual_discovered = stats.get("actual_discovered_candidates", len(cached[0].clips) if cached[0].clips else 0)
            discovery_complete = stats.get("discovery_complete", False)
            required_discovery = settings_dump.get("clip_count", 0)
        
            # Accept the cache IF it meets required discovery capacity,
            # OR if discovery was formally completed (exhausted passes/timeline),
            # OR if it's an old cache (capacity 0) but it magically has enough actual clips
            
            cache_accepted = False
            if discovery_capacity >= required_discovery:
                cache_accepted = True
            elif discovery_complete:
                cache_accepted = True
            elif discovery_capacity == 0 and actual_discovered >= required_discovery:
                cache_accepted = True
                
            if not cache_accepted:
                logger.info("[PIPELINE] job=%s semantic_cache=insufficient_capacity (cap:%d, actual:%d < req:%d, complete:%s)", 
                            job_id, discovery_capacity, actual_discovered, required_discovery, discovery_complete)
                reusable_local = cached[1]
                cached = None
            else:
                analysis_result, local_analysis, _ = cached
                job["analysis_data"] = local_analysis
                job_store.set(job_id, job)
                logger.info("[PIPELINE] job=%s semantic_cache=hit", job_id)
        
        from app.services.auto_edit_director import AutoEditDirector
        needs_cached_compression = bool(cached and settings_dump.get("auto_edit") and AutoEditDirector.needs_compression(analysis_result, settings_dump))
        if not cached or needs_cached_compression:
            if cached:
                reusable_local = local_analysis
            if not job.get("empty_cache_recovery_attempted"):
                job["empty_cache_recovery_attempted"] = True
                job_store.set(job_id, job)
            remote_file = None
            keep_remote = False
            try:
                upload_target = source_path
                if job["metadata"]["file_size"] > 50 * 1024 * 1024:
                    progress_tracker.update_stage(job_id, "PROXY_GENERATION", "Compressing proxy for analysis")
                    ClipService.update_job_status(job_id, "GENERATING_PROXY")
                    proxy_limit = None
                    if job.get("storage_profile") == "DIRECT_UPLOAD":
                        free = storage_lifecycle.get_disk_usage()["free"]
                        proxy_limit = free - storage_lifecycle.processing_floor_bytes(job) - 32 * 1024 ** 2
                        if proxy_limit < 32 * 1024 ** 2:
                            raise InsufficientStorage("INSUFFICIENT_STORAGE")
                    reuse_proxy = False
                    if proxy_path.exists():
                        try:
                            proxy_meta = FFmpegService.get_metadata(proxy_path)
                            reuse_proxy = (job.get("proxy_source_cache_key") == cache_key and
                                           abs(proxy_meta.duration - job["metadata"]["duration"]) <= 2)
                        except (OSError, RuntimeError, ValueError):
                            pass
                        if not reuse_proxy:
                            # Keep an unverified/partial older artifact untouched.
                            import uuid
                            proxy_path = proxy_path.with_name(f"{proxy_path.stem}-{uuid.uuid4().hex[:8]}.mp4")
                    if not reuse_proxy and not FFmpegService.create_proxy(source_path, proxy_path,
                                                      max_output_bytes=proxy_limit,
                                                      source_duration=job["metadata"]["duration"],
                                                      progress_callback=lambda fraction, message:
                                                          progress_tracker.update_progress(job_id, fraction, message)):
                        raise RuntimeError("ANALYSIS_PROXY_FAILED")
                    job_store.update(job_id, lambda current: current.update(
                        proxy_source_cache_key=cache_key))
                    if proxy_limit is not None:
                        storage_lifecycle.require_processing_space(job)
                        try:
                            proxy_duration = FFmpegService.get_metadata(proxy_path).duration
                        except (OSError, RuntimeError, ValueError) as exc:
                            raise InsufficientStorage("INSUFFICIENT_STORAGE") from exc
                        source_duration = job["metadata"]["duration"]
                        if proxy_duration + 2 < source_duration:
                            raise InsufficientStorage("INSUFFICIENT_STORAGE")
                    upload_target = proxy_path
                if reusable_local is not None:
                    job_store.update(job_id, lambda current: current.update(analysis_data=reusable_local))
                else:
                    ClipService.run_local_analysis(job_id, upload_target,
                                                   job["metadata"]["duration"])
                job = job_store.get(job_id)
                local_analysis = job["analysis_data"]
                settings_dump["local_analysis"] = json.dumps({
                    key: value[:60] for key, value in local_analysis.items()})
                settings_dump["source_duration"] = job["metadata"]["duration"]
                on_stage("UPLOADING_TO_AI", "Uploading analysis video to Gemini")
                try:
                    def on_upload_retry(msg):
                        progress_tracker.update_progress(job_id, 0, msg)
                    remote_file = self._analysis_file(gemini_service, job_id, cache_key, upload_target, on_upload_retry)
                    on_stage("ANALYZING_WITH_AI", "Understanding source video")
                    if not cached:
                        def on_checkpoint(stage, data):
                            data["checkpoint_cache_key"] = cache_key
                            job_store.update(job_id, lambda current: current.update(data))
                        
                        checkpoints = {}
                        if job.get("checkpoint_cache_key") == cache_key:
                            if "source_map" in job:
                                checkpoints["source_map"] = job["source_map"]
                                checkpoints["source_map_model"] = job.get("source_map_model", job.get("analysis_models", {}).get("source_map"))
                            if "semantic_discovery" in job:
                                checkpoints["semantic_discovery"] = job["semantic_discovery"]
                                checkpoints["timeline_audit"] = job.get("timeline_audit", [])
                                checkpoints["discovery_model"] = job.get("discovery_model", job.get("analysis_models", {}).get("broad"))
                            if "supplemental_checkpoint" in job:
                                checkpoints["supplemental_checkpoint"] = job["supplemental_checkpoint"]
                        else:
                            # clear stale checkpoints
                            job.pop("source_map", None)
                            job.pop("semantic_discovery", None)
                            job.pop("checkpoint_cache_key", None)
                            
                        analysis_result = gemini_service.analyze_video(remote_file, settings_dump, checkpoints, on_checkpoint)
                        # Broad/enriched discovery is durable before any essential compression.
                        cache.save(cache_key, analysis_result, local_analysis, settings_dump.get("clip_count", 0), len(analysis_result.clips), getattr(analysis_result, "discovery_complete", False), getattr(analysis_result, "discovery_passes_completed", 1))
                    if settings_dump.get("auto_edit"):
                        analysis_result = AutoEditDirector.compress(gemini_service, remote_file, analysis_result, settings_dump)
                        keep_remote = any(c.compression_status == "SERVICE_UNAVAILABLE" for c in analysis_result.clips)
                except Exception as exc:
                    if not settings_dump.get("exact_clip_count"):
                        raise
                    logger.warning("Exact-count AI pass unavailable for job %s: type=%s",
                                   job_id, type(exc).__name__)
                    from app.models.schemas import GeminiAnalysisResponse
                    analysis_result = GeminiAnalysisResponse(
                        source_summary="AI analysis was unavailable. Review all timed source excerpts.",
                        clips=[], analysis_version=PROMPT_VERSION)
                if analysis_result.source_summary != \
                        "AI analysis was unavailable. Review all timed source excerpts.":
                    cache.save(cache_key, analysis_result, local_analysis, settings_dump.get("clip_count", 0), len(analysis_result.clips), getattr(analysis_result, "discovery_complete", False), getattr(analysis_result, "discovery_passes_completed", 1))
            except Exception as exc:
                from app.services.gemini_requests import transient_failure
                keep_remote = transient_failure(exc)
                raise
            finally:
                if remote_file and not keep_remote:
                    gemini_service.delete_video(remote_file.name)
                    job_store.update(job_id, lambda current: current.update(
                        remote_gemini_file={"name": remote_file.name, "cache_key": cache_key, "state": "DELETED"}))
                if proxy_path.exists() and not keep_remote and not job.get("preserve_analysis_artifacts"):
                    proxy_path.unlink(missing_ok=True)

        progress_tracker.update_progress(job_id, 1.0)
        on_stage("VALIDATING_CANDIDATES", "Validating semantic candidates")
        timeline = timeline_audit(analysis_result, job["metadata"]["duration"])
        job_store.update(job_id, lambda current: current.update(timeline_audit=timeline))
        if any(not item["valid"] for item in timeline):
            raise InvalidSemanticTimeline(timeline)
        decisions = []
        job_store.update(job_id, lambda current: current.update(
            semantic_candidates=[c.model_dump() for c in analysis_result.clips],
            analysis_retryable=any(c.compression_status == "SERVICE_UNAVAILABLE" for c in analysis_result.clips)))

        if settings_dump.get("auto_edit"):
            from app.services.auto_edit_director import AutoEditDirector
            valid_candidates, auto_audit, quality_candidate_count, decisions = AutoEditDirector.select(
                analysis_result, job["metadata"]["duration"], job["analysis_data"], target_count=settings_dump.get("target_clip_count"))
        else:
            valid_candidates = ClipService.validate_candidates(
                analysis_result.clips,
                job["metadata"]["duration"],
                job["analysis_data"],
                analysis_result.events,
                analysis_result.narrative_relationships,
                decisions=decisions,
            )
        if settings_dump.get("exact_clip_count"):
            from app.services.count_fill import fill_requested_count
            target = int(settings_dump.get("clip_count", 5))
            min_seconds = int(settings_dump.get("min_clip_seconds", 30))
            max_seconds = int(settings_dump.get("max_clip_seconds", 60))
            valid_candidates = [candidate for candidate in valid_candidates if
                                min_seconds <= sum(segment.end_seconds - segment.start_seconds
                                                   for segment in candidate.source_segments) <= max_seconds]
            valid_candidates = fill_requested_count(
                valid_candidates, target, job["metadata"]["duration"],
                min_seconds, max_seconds,
                job["analysis_data"])
            if len(valid_candidates) < target:
                valid_candidates = fill_requested_count(
                    [], target, job["metadata"]["duration"],
                    min_seconds, max_seconds,
                    job["analysis_data"])
            if len(valid_candidates) < target:
                raise ValueError("EXACT_COUNT_UNAVAILABLE")

        job = job_store.get(job_id) # reload
        job["candidates"] = [c.model_dump() for c in valid_candidates]
        job["source_summary"] = analysis_result.source_summary
        job["source_map"] = analysis_result.source_map
        job["analysis_models"] = analysis_result.analysis_models
        job["analysis_warnings"] = analysis_result.analysis_warnings
        job["candidate_decisions"] = decisions
        job["selection_funnel"] = {
            "raw_events": len(analysis_result.events),
            "raw_candidates": len(analysis_result.clips),
            "timeline_valid": sum(item["valid"] for item in timeline),
            "deep_complete": sum(c.deep_analysis_status == "COMPLETE" for c in analysis_result.clips),
            "quality_valid": sum(item["decision"] in {"SELECTED", "DUPLICATE"} for item in decisions),
            "final": len(valid_candidates),
        }
        if settings_dump.get("auto_edit"):
            from app.services.auto_edit_director import AUTO_EDIT_VERSION
            job["auto_edit_version"] = AUTO_EDIT_VERSION
            job["selection_audit"] = auto_audit
            job["target_metrics"] = {
                "count_mode": settings_dump.get("count_mode", "AUTO"),
                "requested_clip_count": settings_dump.get("target_clip_count"),
                "raw_candidate_count": len(analysis_result.clips),
                "quality_candidate_count": quality_candidate_count,
                "final_clip_count": len(valid_candidates)
            }
        job["narrative_events"] = [event.model_dump() for event in analysis_result.events]
        job["narrative_relationships"] = [rel.model_dump()
                                          for rel in analysis_result.narrative_relationships]
        job["source_visual_inserts"] = [insert.model_dump()
                                        for insert in analysis_result.source_visual_inserts
                                        if 0 <= insert.source_start < insert.source_end <=
                                        job["metadata"]["duration"]]
        if (not settings_dump.get("exact_clip_count") and
                len(valid_candidates) < int(settings_dump.get("clip_count", 5))):
            job["selection_shortfall_reason"] = "FEWER_CANDIDATES_PASSED_QUALITY_FLOOR"
        else:
            job.pop("selection_shortfall_reason", None)
        job["analysis_version"] = analysis_result.analysis_version
        from app.services.editorial_service import EDITOR_VERSION
        job["editor_version"] = EDITOR_VERSION
        job_store.set(job_id, job)
        logger.info("[AUTO_EDIT] job=%s analysis_version=%s models=%s", job_id,
                    job.get("analysis_version"), job.get("analysis_models"))

        logger.info(f"[PIPELINE] job={job_id} stage=GEMINI_COMPLETE")
        logger.info(f"[PIPELINE] job={job_id} candidates_raw={len(analysis_result.clips)}")
        logger.info(f"[PIPELINE] job={job_id} stage=VALIDATING_CANDIDATES")
        logger.info(f"[PIPELINE] job={job_id} candidates_valid={len(valid_candidates)}")

        if len(valid_candidates) == 0:
            if any(d.get("decision") == "COMPRESSION_SERVICE_UNAVAILABLE" for d in decisions):
                raise ValueError("AI_COMPRESSION_SERVICE_UNAVAILABLE")
            raise ValueError("NO_SUITABLE_CLIPS" if not analysis_result.clips
                             else "ALL_CANDIDATES_REJECTED")

        progress_tracker.finish_job(job_id)

        # Check auto_render
        if job.get("auto_render", False):
            logger.info(f"[PIPELINE] job={job_id} stage=START_RENDER_PHASE")

            # Populate render_request automatically
            job["render_request"] = {
                "candidates": [c.model_dump() for c in valid_candidates],
                "settings": job.get("analysis_settings", {})
            }
            job_store.set(job_id, job)

            ClipService.update_job_status(job_id, "QUEUED_RENDER")
        else:
            ClipService.update_job_status(job_id, "READY_TO_RENDER")


    @staticmethod
    def _analysis_file(service, job_id, cache_key, local_path, on_retry=None):
        """One job-owned remote file; retry the same active file, never a global cache."""
        from app.services.gemini_requests import classify_error, GenerationFailure, transient_failure
        job = job_store.get(job_id) or {}
        saved = job.get("remote_gemini_file") or {}
        reason = "NO_RECORDED_REMOTE_FILE"
        if saved.get("cache_key") == cache_key and saved.get("state") != "DELETED" and saved.get("name"):
            try:
                remote = service.client.files.get(name=saved["name"])
                if str(getattr(remote.state, "value", remote.state)) == "ACTIVE":
                    job_store.update(job_id, lambda current: current.setdefault("remote_file_history", []).append(
                        {"action": "REUSED_ACTIVE", "timestamp": time.time()}))
                    return remote
                reason = "REMOTE_FILE_NOT_ACTIVE"
            except Exception as exc:
                if classify_error(exc)[0] != 404:
                    if transient_failure(exc):
                        raise GenerationFailure({"files.get": exc}) from exc
                    raise
                reason = "REMOTE_FILE_EXPIRED_OR_DELETED"
        elif saved.get("state") == "DELETED":
            reason = "PREVIOUS_REMOTE_FILE_DELETED"
        remote = service.upload_video(local_path, on_retry=on_retry)
        job_store.update(job_id, lambda current: current.update(remote_gemini_file={
            "name": remote.name, "cache_key": cache_key, "state": "ACTIVE"}))
        job_store.update(job_id, lambda current: current.setdefault("remote_file_history", []).append(
            {"action": "UPLOADED", "reason": reason, "timestamp": time.time()}))
        return remote

    def _run_render(self, job: dict):
        from app.services.disk_cleaner import free_disk_space
        free_disk_space(job)
        job_id = job["id"]
        from app.models.schemas import ClipCandidate, RenderSettings

        req_cands = [ClipCandidate(**c) for c in job["render_request"]["candidates"]]
        req_settings = RenderSettings(**job["render_request"]["settings"])

        total_time = sum([s.end_seconds - s.start_seconds for c in req_cands for s in c.source_segments])
        progress_tracker.start_job(job_id, total_time, "RENDER")
        progress_tracker.update_stage(job_id, "RENDERING", "Initializing render engine")

        ClipService.process_renders(job_id, req_cands, req_settings)
        progress_tracker.finish_job(job_id)

worker = PipelineWorker()
