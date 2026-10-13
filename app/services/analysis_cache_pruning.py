"""Bound only generated analysis JSON inside the configured cache directory."""

from pathlib import Path

from app.config import config


def prune_analysis_cache(root: Path, max_files: int = 60,
                         max_bytes: int = 48 * 1024 * 1024) -> None:
    cache_root = (config.DATA_ROOT / "cache").resolve()
    resolved_root = root.resolve()
    if root.is_symlink() or not resolved_root.is_relative_to(cache_root):
        return
    files = []
    for path in root.glob("*.json"):
        if path.is_symlink() or path.parent.resolve() != resolved_root:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            files.append((stat.st_mtime_ns, stat.st_size, path))
    files.sort(reverse=True)
    total = 0
    for index, (_, size, path) in enumerate(files):
        if index < max_files and total + size <= max_bytes:
            total += size
        else:
            try:
                if (not path.is_symlink() and
                        path.resolve().is_relative_to(cache_root)):
                    path.unlink(missing_ok=True)
            except OSError:
                pass
