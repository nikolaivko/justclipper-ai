"""Resolve a persisted job render and its canonical output file."""

import re
from pathlib import Path

from app.config import config
from app.services.job_store import job_store


class RenderResolutionError(Exception):
    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def resolve_render(job_id: str, rank: int) -> tuple[dict, dict, Path]:
    # JobStore builds a filename from this value, so reject separators and dots.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise RenderResolutionError("JOB_NOT_FOUND", 404)

    job = job_store.get(job_id)
    if not job or job.get("id") != job_id:
        raise RenderResolutionError("JOB_NOT_FOUND", 404)

    render = next(
        (r for r in job.get("renders", []) if r.get("rank") == rank), None
    )
    if render is None:
        raise RenderResolutionError("RENDER_NOT_FOUND", 404)
    if not render.get("success"):
        raise RenderResolutionError("RENDER_NOT_COMPLETED", 400)

    output_path = render.get("output_path")
    if not output_path:
        raise RenderResolutionError("OUTPUT_FILE_MISSING", 404)

    path = Path(output_path).resolve()
    if path.suffix.lower() != ".mp4" or not path.is_relative_to(config.OUTPUT_DIR.resolve()):
        raise RenderResolutionError("INVALID_OUTPUT", 400)
    if not path.is_file():
        raise RenderResolutionError("OUTPUT_FILE_MISSING", 404)

    return job, render, path
