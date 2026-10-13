"""Cache selected-EDL visual signals across render style retries."""

import hashlib
import json
import os
from pathlib import Path

from app.config import config


class VisualAnalysisCache:
    VERSION = "visual-v2"

    def __init__(self, root: Path | None = None):
        self.root = root or config.DATA_CACHE / "visual"

    def key(self, source: Path, segments: list[tuple], source_key: str | None) -> str:
        stat = source.stat()
        # Source key is a SHA-256-based semantic key for current jobs. Legacy jobs
        # also include path, size and mtime, preventing cache reuse after replacement.
        identity = source_key or f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
        descriptor = json.dumps({"version": self.VERSION, "source": identity,
                                 "segments": segments}, sort_keys=True)
        return hashlib.sha256(descriptor.encode()).hexdigest()

    def load(self, key: str) -> dict | None:
        try:
            record = json.loads((self.root / f"{key}.json").read_text())
            if record.get("version") == self.VERSION:
                return record["signals"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def save(self, key: str, signals: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{key}.json"
        temp = self.root / f".{key}.{os.getpid()}.tmp"
        try:
            temp.write_text(json.dumps({"version": self.VERSION, "signals": signals}))
            os.replace(temp, path)
            from app.services.analysis_cache_pruning import prune_analysis_cache
            prune_analysis_cache(self.root)
        finally:
            temp.unlink(missing_ok=True)
