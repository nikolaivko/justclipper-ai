"""Versioned source-hash cache for expensive semantic and local analysis."""

import hashlib
import json
import os
from pathlib import Path

from app.config import config
from app.models.schemas import GeminiAnalysisResponse
from app.services.gemini_service import PROMPT_VERSION, SUPPLEMENTAL_CONTEXT_VERSION


class SemanticCache:
    def __init__(self, root: Path | None = None):
        self.root = root or config.DATA_CACHE / "semantic"

    def key(self, source: Path, settings: dict) -> str:
        source_hash = hashlib.sha256()
        with source.open("rb") as file:
            for chunk in iter(lambda: file.read(4 * 1024 * 1024), b""):
                source_hash.update(chunk)
        semantic_settings = {key: settings.get(key) for key in (
            "min_clip_seconds", "max_clip_seconds", "editorial_guidance")}
        descriptor_data = {"source_sha256": source_hash.hexdigest(),
                           "analysis_version": PROMPT_VERSION,
                           "analysis_model": config.GEMINI_MODEL,
                           "schema_version": 4,
                           "deep_model": config.GEMINI_DEEP_MODEL,
                           "deep_limit": config.AUTO_EDIT_DEEP_CANDIDATES,
                           "auto_edit": bool(settings.get("auto_edit")),
                           "settings": semantic_settings}
        if settings.get("exact_clip_count"):
            descriptor_data["exact_count_strategy"] = 1
        descriptor = json.dumps(descriptor_data, sort_keys=True)
        return hashlib.sha256(descriptor.encode()).hexdigest()

    def load(self, key: str) -> tuple[GeminiAnalysisResponse, dict, dict] | None:
        path = self.root / f"{key}.json"
        try:
            record = json.loads(path.read_text())
            if record.get("analysis_version") != PROMPT_VERSION:
                return None
            if ((record.get("discovery_passes_completed", 1) > 1 or
                 any(str(c.get("discovery_pass", "")).startswith("SUPPLEMENTAL")
                     for c in record["result"].get("clips", []))) and
                    record.get("supplemental_context_version") != SUPPLEMENTAL_CONTEXT_VERSION):
                return None
            return (GeminiAnalysisResponse.model_validate(record["result"]),
                    record["local_analysis"],
                    {
                        "discovery_capacity": record.get("discovery_capacity", 0),
                        "actual_discovered_candidates": record.get("actual_discovered_candidates", 0),
                        "discovery_complete": record.get("discovery_complete", False),
                        "discovery_passes_completed": record.get("discovery_passes_completed", 1)
                    })
        except (OSError, ValueError, KeyError):
            return None

    def save(self, key: str, result: GeminiAnalysisResponse, local_analysis: dict, requested_discovery_capacity: int = 0, actual_discovered_candidates: int = 0, discovery_complete: bool = False, discovery_passes_completed: int = 1) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{key}.json"
        temp = self.root / f".{key}.{os.getpid()}.tmp"
        try:
            temp.write_text(json.dumps({"analysis_version": PROMPT_VERSION,
                                        "supplemental_context_version": SUPPLEMENTAL_CONTEXT_VERSION,
                                        "result": result.model_dump(exclude_unset=True),
                                        "local_analysis": local_analysis, "discovery_capacity": requested_discovery_capacity, "actual_discovered_candidates": actual_discovered_candidates, "discovery_complete": discovery_complete, "discovery_passes_completed": discovery_passes_completed}))
            os.replace(temp, path)
            from app.services.analysis_cache_pruning import prune_analysis_cache
            prune_analysis_cache(self.root)
        finally:
            temp.unlink(missing_ok=True)
