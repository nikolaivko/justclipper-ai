import os
import shutil
import uuid
import re
from pathlib import Path
from fastapi import UploadFile
from app.config import config

class FileService:
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        # Keep alphanumeric, dot, dash, underscore
        filename = os.path.basename(filename)
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', filename)
        return sanitized

    @staticmethod
    def get_unique_job_id() -> str:
        return str(uuid.uuid4())[:8]

    @staticmethod
    async def save_upload(file: UploadFile, job_id: str) -> Path:
        safe_name = FileService.sanitize_filename(file.filename or f"upload_{job_id}.mp4")
        # Ensure it has a safe name format: <job_id>_<safename>
        unique_name = f"{job_id}_{safe_name}"
        dest_path = config.INPUT_DIR / unique_name
        
        from app.services.storage_lifecycle import storage_lifecycle
        try:
            with open(dest_path, "wb") as buffer:
                while chunk := file.file.read(1024 * 1024):
                    storage_lifecycle.require_upload_space(len(chunk))
                    buffer.write(chunk)
        except Exception:
            dest_path.unlink(missing_ok=True)
            raise
            
        return dest_path

    @staticmethod
    async def save_stream(stream, job_id: str, filename: str, expected_bytes: int) -> Path:
        """Write one raw request body to input without multipart disk spooling."""
        from app.services.storage_lifecycle import storage_lifecycle

        safe_name = FileService.sanitize_filename(filename)
        if not safe_name or expected_bytes <= 0:
            raise ValueError("INVALID_UPLOAD")
        dest_path = config.INPUT_DIR / f"{job_id}_{safe_name}"
        created = False
        complete = False
        written = 0
        try:
            with dest_path.open("xb") as buffer:
                created = True
                async for chunk in stream:
                    if not chunk:
                        continue
                    if written + len(chunk) > expected_bytes:
                        raise ValueError("UPLOAD_SIZE_MISMATCH")
                    storage_lifecycle.require_upload_space(expected_bytes - written)
                    buffer.write(chunk)
                    written += len(chunk)
                if written != expected_bytes:
                    raise ValueError("UPLOAD_SIZE_MISMATCH")
            complete = True
            return dest_path
        finally:
            if created and not complete:
                dest_path.unlink(missing_ok=True)

    @staticmethod
    def get_proxy_path(source_path: Path, job_id: str) -> Path:
        return config.TEMP_DIR / f"{job_id}_proxy.mp4"

    @staticmethod
    def get_output_path(source_filename: str, clip_rank: int, title: str, job_id: str = "") -> Path:
        safe_title = FileService.sanitize_filename(title).replace('.', '')[:100]
        prefix = f"{job_id}_" if job_id else ""
        out_name = f"{prefix}clip_{clip_rank:02d}_{safe_title}.mp4"
        return config.OUTPUT_DIR / out_name

    @staticmethod
    def clean_temp_files():
        for item in config.TEMP_DIR.iterdir():
            if item.is_file() and item.name != ".gitkeep":
                try:
                    item.unlink()
                except Exception:
                    pass
