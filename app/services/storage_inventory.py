"""Read-only, path-free aggregate inventory of the persistent volume."""

import os
import time
from collections import defaultdict
from pathlib import Path

from app.config import config
from app.services.job_store import job_store
from app.services.source_store import source_store
from app.services.storage_lifecycle import ACTIVE_STATUSES, storage_lifecycle


def storage_inventory() -> dict:
    root = storage_lifecycle.root
    usage = storage_lifecycle.get_disk_usage()
    acquisition_required = int((config.MIN_FREE_DISK_GB +
                                max(0, config.ACQUISITION_WORKING_RESERVE_GB)) * 1024 ** 3)
    upload_floor = int((config.DIRECT_UPLOAD_MIN_FREE_GB +
                        max(0, config.UPLOAD_WORKING_RESERVE_GB)) * 1024 ** 3)
    categories = defaultdict(lambda: {"bytes": 0, "files": 0})
    roles = defaultdict(lambda: {"bytes": 0, "files": 0})
    references = {}
    legacy_sources = set()
    expired_final_dry_run = {"files": 0, "bytes": 0,
                             "missing_timestamps_files": 0,
                             "missing_timestamps_bytes": 0}
    now = time.time()
    for job in job_store.get_all():
        status = job.get("status")
        source = job.get("filepath")
        if source:
            references[str(source)] = "active" if status in ACTIVE_STATUSES else (
                "failed" if status == "FAILED" else "source")
            if job.get("lifecycle_version") != 1:
                legacy_sources.add(str(source))
        for render in job.get("renders") or []:
            output = render.get("output_path")
            if output:
                references[str(output)] = "active" if status in ACTIVE_STATUSES else (
                    "uploaded" if render.get("publish_status") == "UPLOADED" and
                    render.get("youtube_video_id") else "retained_final")
            if (status != "COMPLETED" or not render.get("success") or
                    render.get("publish_status") or not output or
                    job.get("bulk_publish_status") == "RUNNING" or
                    type(render.get("rank")) is not int):
                continue
            try:
                path = storage_lifecycle._render_file(output, job["id"], render["rank"])
                size = path.stat().st_size
            except (ValueError, OSError, KeyError):
                continue
            completed = render.get("completed_at") or job.get("completed_at")
            if not isinstance(completed, (int, float)) or completed <= 0:
                completed = None
            if not completed:
                expired_final_dry_run["missing_timestamps_files"] += 1
                expired_final_dry_run["missing_timestamps_bytes"] += size
            if now - (completed or path.stat().st_mtime) >= config.FINAL_OUTPUT_RETENTION_HOURS * 3600:
                from app.services.youtube_publisher import youtube_publisher
                if youtube_publisher.validate_file(path):
                    expired_final_dry_run["files"] += 1
                    expired_final_dry_run["bytes"] += size
    for asset in source_store.get_all():
        if asset.source_path and str(asset.source_path) not in references:
            references[str(asset.source_path)] = "registered_source"

    large = []
    ambiguous_legacy_bytes = 0
    planned = storage_lifecycle.plan_reconciliation()
    cleanable_paths = {str(action["path"]) for action in planned}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            path = Path(entry.path)
            parts = path.relative_to(root).parts
            top = parts[0] if len(parts) > 1 else "root"
            group = top + ("/" + parts[1] if len(parts) > 2 else "")
            categories[top]["bytes"] += size
            categories[top]["files"] += 1
            if group != top:
                categories[group]["bytes"] += size
                categories[group]["files"] += 1
            if top in ("youtube", "config"):
                role = "protected"
            elif str(path) in cleanable_paths:
                role = "safe_cleanable"
            elif str(path) in references:
                role = references[str(path)]
            elif top in ("input", "sources", "output", "temp", "cache", "models"):
                role = "unclassified"
            else:
                role = "metadata_or_other"
            roles[role]["bytes"] += size
            roles[role]["files"] += 1
            if (str(path) not in cleanable_paths and role != "active" and
                    (str(path) in legacy_sources or
                     (role == "unclassified" and top in
                      ("input", "sources", "output", "temp")))):
                ambiguous_legacy_bytes += size
            if size >= 50 * 1024 * 1024:
                large.append({"category": top, "bytes": size, "role": role})
    return {
        "total_bytes": usage["total"], "used_bytes": usage["used"],
        "free_bytes": usage["free"],
        "acquisition_required_free_bytes": acquisition_required,
        "acquisition_preflight_ok": usage["free"] >= acquisition_required,
        "max_stream_upload_bytes": max(0, usage["free"] - upload_floor),
        "percent_used": round(100 * usage["used"] / usage["total"], 1) if usage["total"] else 0,
        "categories": dict(categories), "roles": dict(roles),
        "safe_cleanable_bytes": sum(action["bytes"] for action in planned),
        "safe_cleanable_files": len(planned),
        "active_bytes": roles["active"]["bytes"],
        "retained_final_output_bytes": roles["retained_final"]["bytes"],
        "expired_unpublished_final_dry_run": expired_final_dry_run,
        "ambiguous_legacy_bytes": ambiguous_legacy_bytes,
        "safe_cleanable_by_kind": {
            kind: {"bytes": sum(a["bytes"] for a in planned if a["kind"] == kind),
                   "files": sum(a["kind"] == kind for a in planned)}
            for kind in sorted({action["kind"] for action in planned})},
        "large_files": sorted(large, key=lambda item: item["bytes"], reverse=True)[:30],
    }
