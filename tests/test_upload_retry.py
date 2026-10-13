import pytest
from unittest.mock import MagicMock, Mock, patch
from pathlib import Path
from app.services.gemini_service import GeminiService
from google.genai import errors
import requests

class FakeAPIError(errors.APIError):
    def __init__(self, code, message):
        resp = requests.Response()
        resp.status_code = code
        resp.reason = message
        resp._content = b"{}"
        super().__init__(code, response=resp)

def test_upload_retries_transient_502():
    svc = GeminiService()
    svc.client = MagicMock()
    
    attempts = {"count": 0}
    
    def side_effect(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise FakeAPIError(502, "Bad Gateway")
        video_file = MagicMock()
        video_file.state = "ACTIVE"
        return video_file

    svc.client.files.upload = Mock(side_effect=side_effect)
    
    with patch("app.services.gemini_service.time.sleep") as mock_sleep:
        with patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'stat') as mock_stat:
            mock_stat.return_value.st_size = 1000
            
            result = svc.upload_video(Path("test.mp4"))
            
    assert attempts["count"] == 3
    assert mock_sleep.call_count == 2
    assert result.state == "ACTIVE"

def test_upload_no_retry_400():
    svc = GeminiService()
    svc.client = MagicMock()
    
    attempts = {"count": 0}
    
    def side_effect(*args, **kwargs):
        attempts["count"] += 1
        raise FakeAPIError(400, "Invalid Argument")

    svc.client.files.upload = Mock(side_effect=side_effect)
    
    with patch("app.services.gemini_service.time.sleep") as mock_sleep:
        with patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'stat') as mock_stat:
            mock_stat.return_value.st_size = 1000
            
            with pytest.raises(FakeAPIError):
                svc.upload_video(Path("test.mp4"))
                
    assert attempts["count"] == 1
    assert mock_sleep.call_count == 0

def test_upload_raises_on_max_retries():
    svc = GeminiService()
    svc.client = MagicMock()
    
    def side_effect(*args, **kwargs):
        raise FakeAPIError(503, "Service Unavailable")

    svc.client.files.upload = Mock(side_effect=side_effect)
    
    with patch("app.services.gemini_service.time.sleep") as mock_sleep:
        with patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'stat') as mock_stat:
            mock_stat.return_value.st_size = 1000
            
            with pytest.raises(RuntimeError, match="Gemini video upload is temporarily unavailable after multiple attempts"):
                svc.upload_video(Path("test.mp4"))
                
    assert svc.client.files.upload.call_count == 4
    assert mock_sleep.call_count == 3
