import pytest
from app.services.file_service import FileService

def test_sanitize_filename():
    assert FileService.sanitize_filename("my video.mp4") == "my_video.mp4"
    assert FileService.sanitize_filename("../../etc/passwd") == "passwd"
    assert FileService.sanitize_filename("test!@#$.mp4") == "test____.mp4"
    assert FileService.sanitize_filename("normal_file-123.mov") == "normal_file-123.mov"

def test_get_unique_job_id():
    job_id = FileService.get_unique_job_id()
    assert len(job_id) == 8
    assert job_id.isalnum() or "-" in job_id

