from app.config import config
import json
import time
import os
from pathlib import Path
from typing import Dict, Optional

class ProgressTracker:
    def __init__(self, project_root: Path):
        self.cache_dir = config.DATA_CACHE / "performance"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timings_file = self.cache_dir / "stage_timings.json"
        self.timings = self._load_timings()
        
        self.active_jobs = {}

    def _load_timings(self) -> dict:
        if self.timings_file.exists():
            try:
                with open(self.timings_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "gemini_sec_per_min": [30.0],
            "yolo_sec_per_frame": [0.03],
            "scene_sec_per_min": [2.0],
            "render_sec_per_sec": [0.2]
        }

    def _save_timings(self):
        try:
            with open(self.timings_file, "w") as f:
                json.dump(self.timings, f)
        except Exception:
            pass

    def _add_timing(self, key: str, value: float):
        if key not in self.timings:
            self.timings[key] = []
        self.timings[key].append(value)
        # Keep last 10 for rolling median/average
        if len(self.timings[key]) > 10:
            self.timings[key].pop(0)
        self._save_timings()

    def _get_estimate(self, key: str, default: float) -> float:
        history = self.timings.get(key, [])
        if not history:
            return default
        # Return median
        s = sorted(history)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 != 0 else (s[mid - 1] + s[mid]) / 2.0

    def start_job(self, job_id: str, total_duration_sec: float, mode: str):
        self.active_jobs[job_id] = {
            "start_time": time.time(),
            "total_duration_sec": total_duration_sec,
            "mode": mode,  # "ANALYSIS" or "RENDER"
            "stages": [],
            "current_stage": None,
            "stage_start_time": None,
            "stage_progress": 0.0,
            "overall_progress": 0.0,
            "eta_seconds": None,
            "status_message": "Initializing...",
            "last_update_time": time.time()
        }
        
    def update_stage(self, job_id: str, stage_name: str, status_message: str):
        job = self.active_jobs.get(job_id)
        if not job: return
        
        now = time.time()
        # Record previous stage timing if deterministic
        if job["current_stage"] == "OBJECT_TRACKING" and job["stage_progress"] >= 0.95:
            elapsed = now - job["stage_start_time"]
            frames = job["total_duration_sec"] * 30 # approx 30 fps
            if frames > 0:
                self._add_timing("yolo_sec_per_frame", elapsed / frames)
                
        elif job["current_stage"] == "PROXY_GENERATION" and job["stage_progress"] >= 0.95:
            elapsed = now - job["stage_start_time"]
            if job["total_duration_sec"] > 0:
                self._add_timing("proxy_sec_per_sec", elapsed / job["total_duration_sec"])
        elif job["current_stage"] == "GEMINI_ANALYSIS" and job["stage_progress"] >= 0.95:
            elapsed = now - job["stage_start_time"]
            mins = job["total_duration_sec"] / 60.0
            if mins > 0:
                self._add_timing("gemini_sec_per_min", elapsed / mins)
                
        job["current_stage"] = stage_name
        job["status_message"] = status_message
        job["stage_start_time"] = now
        job["stage_progress"] = 0.0
        job["last_update_time"] = now
        self._calculate_eta_and_overall(job)
        
    def update_progress(self, job_id: str, stage_progress: float, status_message: str = None):
        job = self.active_jobs.get(job_id)
        if not job: return
        
        # Clamp stage progress
        job["stage_progress"] = max(0.0, min(1.0, stage_progress))
        if status_message:
            job["status_message"] = status_message
            
        now = time.time()
        job["last_update_time"] = now
        
        # Calculate overall and ETA
        self._calculate_eta_and_overall(job)
        
    def _calculate_eta_and_overall(self, job: dict):
        mode = job["mode"]
        duration = job["total_duration_sec"]
        
        if mode == "ANALYSIS":
            stages = ["PREPARING_VIDEO", "PROXY_GENERATION", "SCENE_DETECTION",
                      "UPLOADING_TO_AI", "WAITING_FOR_AI_FILE", "ANALYZING_WITH_AI",
                      "BUILDING_CANDIDATES", "VALIDATING_CANDIDATES"]
            weights = {
                "PREPARING_VIDEO": 0.05,
                "PROXY_GENERATION": 0.20,
                "SCENE_DETECTION": 0.10,
                "UPLOADING_TO_AI": 0.05,
                "WAITING_FOR_AI_FILE": 0.05,
                "ANALYZING_WITH_AI": 0.35,
                "BUILDING_CANDIDATES": 0.15,
                "VALIDATING_CANDIDATES": 0.05
            }
            
            # Overall progress
            current_idx = stages.index(job["current_stage"]) if job["current_stage"] in stages else 0
            overall = 0.0
            for i in range(current_idx):
                overall += weights.get(stages[i], 0.0)
            overall += weights.get(job["current_stage"], 0.0) * job["stage_progress"]
            
            # Monotonic overall progress
            job["overall_progress"] = max(job["overall_progress"], overall)
            
            # Fixed display weights are not processing speed. Estimate the current
            # stage from its timestamps, then add the remaining stages separately.
            estimates = {
                "PREPARING_VIDEO": 5.0,
                "PROXY_GENERATION": self._get_estimate("proxy_sec_per_sec", 1.0) * duration,
                "SCENE_DETECTION": self._get_estimate("scene_sec_per_min", 2.0) * duration / 60,
                "UPLOADING_TO_AI": 30.0,
                "WAITING_FOR_AI_FILE": 30.0,
                "ANALYZING_WITH_AI": self._get_estimate("gemini_sec_per_min", 30.0) * duration / 60,
                "BUILDING_CANDIDATES": 30.0,
                "VALIDATING_CANDIDATES": 5.0,
            }
            elapsed = time.time() - (job["stage_start_time"] or job["start_time"])
            fraction = job["stage_progress"]
            if fraction >= .01 and elapsed >= 1:
                remaining = elapsed * (1 - fraction) / fraction
            else:
                remaining = max(0, estimates.get(job["current_stage"], 0) - elapsed)
            job["eta_seconds"] = remaining + sum(estimates[s] for s in stages[current_idx + 1:])
            if job["current_stage"] in {"UPLOADING_TO_AI", "WAITING_FOR_AI_FILE",
                                         "ANALYZING_WITH_AI", "BUILDING_CANDIDATES"}:
                job["eta_seconds"] = None
                
        elif mode == "RENDER":
            job["overall_progress"] = max(job["overall_progress"], job["stage_progress"])
            elapsed = time.time() - job["start_time"]
            
            if job["overall_progress"] > 0.02:
                observed_total = elapsed / job["overall_progress"]
                job["eta_seconds"] = max(0, observed_total - elapsed)
            else:
                estimated_total = self._get_estimate("render_sec_per_sec", 0.2) * duration
                job["eta_seconds"] = max(0, estimated_total - elapsed)

    def fail_job(self, job_id: str, reason: str):
        job = self.active_jobs.get(job_id)
        if not job: return
        job["eta_seconds"] = None
        job["status_message"] = reason
        job["finished_at"] = time.time()

    def finish_job(self, job_id: str):
        job = self.active_jobs.get(job_id)
        if not job: return
        
        job["overall_progress"] = 1.0
        job["stage_progress"] = 1.0
        job["eta_seconds"] = 0
        job["status_message"] = "Completed"
        job["finished_at"] = time.time()
        
        # If it was a render job, record performance
        if job["mode"] == "RENDER" and job["total_duration_sec"] > 0:
            elapsed = time.time() - job["start_time"]
            self._add_timing("render_sec_per_sec", elapsed / job["total_duration_sec"])

    def get_job_progress(self, job_id: str) -> dict:
        job = self.active_jobs.get(job_id)
        if not job: return None
        return {
            "stage": job["current_stage"],
            "stage_progress": job["stage_progress"],
            "overall_progress": job["overall_progress"],
            "elapsed_seconds": job.get("finished_at", time.time()) - job["start_time"],
            "estimated_remaining_seconds": job["eta_seconds"],
            "status_message": job["status_message"]
        }

# Global singleton
progress_tracker = ProgressTracker(config.DATA_ROOT)
