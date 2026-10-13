import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from app.services.youtube_publisher import YouTubePublisher, classify_upload_error

@pytest.fixture
def publisher():
    pub = YouTubePublisher()
    pub.token_file = Path("/tmp/test_token.json")
    return pub

@patch('app.services.youtube_publisher.build')
def test_publish_video_missing_creds(mock_build, publisher):
    # If token_file doesn't exist, it should return REAUTH_REQUIRED
    if publisher.token_file.exists():
        publisher.token_file.unlink()
        
    result = publisher.publish_video(Path("/tmp/fake.mp4"), "Test", "Test desc")
    assert result["status"] == "REAUTH_REQUIRED"
    assert "No valid credentials" in result["error"]

@patch('app.services.youtube_publisher.build')
@patch.object(YouTubePublisher, 'validate_file')
@patch.object(YouTubePublisher, '_get_credentials')
def test_publish_video_invalid_file(mock_creds, mock_validate, mock_build, publisher):
    mock_creds.return_value = MagicMock()
    mock_validate.return_value = False
    
    result = publisher.publish_video(Path("/tmp/fake.mp4"), "Test", "Test desc")
    assert result["status"] == "FAILED"
    assert "Invalid video file" in result["error"]

@patch('app.services.youtube_publisher.build')
@patch('app.services.youtube_publisher.MediaFileUpload')
@patch.object(YouTubePublisher, 'validate_file')
@patch.object(YouTubePublisher, '_get_credentials')
def test_publish_video_success(mock_creds, mock_validate, mock_media, mock_build, publisher):
    mock_creds.return_value = MagicMock()
    mock_validate.return_value = True
    
    mock_youtube = MagicMock()
    mock_build.return_value = mock_youtube
    
    mock_insert = MagicMock()
    mock_youtube.videos().insert.return_value = mock_insert
    
    # Mock chunk upload
    mock_chunk = MagicMock()
    mock_chunk.progress.return_value = 1.0
    mock_insert.next_chunk.side_effect = [(mock_chunk, None), (None, {
        "id": "TEST_ID_123", "status": {"privacyStatus": "public"}})]
    
    result = publisher.publish_video(Path("/tmp/fake.mp4"), "Test", "Test desc")
    assert result["status"] == "UPLOADED"
    assert result["video_id"] == "TEST_ID_123"
    assert result["privacy_status"] == "public"
    assert mock_youtube.videos().insert.call_args.kwargs["body"]["status"]["privacyStatus"] == "public"


@pytest.mark.parametrize("reason,expected", [
    ("quotaExceeded", "API_QUOTA_EXCEEDED"),
    ("dailyLimitExceeded", "API_QUOTA_EXCEEDED"),
    ("uploadLimitExceeded", "CHANNEL_UPLOAD_LIMIT"),
    ("rateLimitExceeded", "RATE_LIMITED"),
    ("invalidCredentials", "REAUTH_REQUIRED"),
])
def test_google_error_reasons_are_sanitized(reason, expected):
    class GoogleError(Exception):
        content = ('{"error":{"errors":[{"reason":"' + reason + '"}]}}').encode()
    assert classify_upload_error(GoogleError("sensitive Google details")) == expected
