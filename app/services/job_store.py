import json
import re
from pathlib import Path
from typing import Dict, Any, List
from app.config import config
from app.services.json_storage import atomic_write_json, record_lock

class JobStore:
    def __init__(self):
        self.jobs_dir = config.DATA_JOBS
        
    def _get_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job_id):
            raise ValueError("INVALID_JOB_ID")
        return self.jobs_dir / f"{job_id}.json"
        
    def get(self, job_id: str) -> Dict[str, Any]:
        try:
            data = json.loads(self._get_path(job_id).read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None
            
    def set(self, job_id: str, job_data: Dict[str, Any], *, durable: bool = False):
        p = self._get_path(job_id)
        with record_lock(p):
            atomic_write_json(p, job_data, durable=durable)

    def update(self, job_id: str, mutate, *, durable: bool = False):
        """Apply a short read/modify/write transaction without losing other fields."""
        p = self._get_path(job_id)
        with record_lock(p):
            job = self.get(job_id)
            if job is None:
                return None
            mutate(job)
            atomic_write_json(p, job, durable=durable)
            return job
            
    def delete(self, job_id: str) -> bool:
        p = self._get_path(job_id)
        try:
            p.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def get_all(self) -> List[Dict[str, Any]]:
        results = []
        for p in self.jobs_dir.glob("*.json"):
            data = self.get(p.stem)
            if data is not None:
                results.append(data)
        return results

job_store = JobStore()
