import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.services.download_service import DownloadService
from app.services.source_rights_service import SourceRightsService
import json

client = TestClient(app)

def test_url_import_rejects_invalid_video_id():
    res = client.post("/api/download/youtube", json={"video_id": "short"})
    assert res.status_code == 400
    assert res.json()["detail"] == "INVALID_VIDEO_ID"

@patch("app.api.routes.SourceRightsService.allows_public_acquisition", return_value=True)
@patch("app.api.routes.youtube_service.get_video_rights")
@patch("app.api.routes.download_service")
def test_url_import_starts_acquisition(mock_ds, mock_rights, mock_allows):
    mock_rights.return_value = {
        "rights_status": "PUBLIC",
        "title": "Test Video",
        "channel": "Test Channel",
        "channel_id": "test_id",
        "privacy_status": "public",
        "upload_status": "processed"
    }
    mock_ds.technical_downloadability.return_value = "OK"
    mock_ds.enqueue_download.return_value = "dl-123"
    
    res = client.post("/api/download/youtube", json={"video_id": "11111111111"})
    assert res.status_code == 200
    assert res.json() == {"dl_id": "dl-123"}
    
    # Check that enqueue was called with force_retry=True
    mock_ds.enqueue_download.assert_called_once()
    kwargs = mock_ds.enqueue_download.call_args[1]
    assert kwargs.get("force_retry") is True

@patch("app.api.routes.youtube_service.get_video_rights")
def test_url_import_respects_rights_blocks(mock_rights):
    mock_rights.return_value = {
        "rights_status": "RESTRICTED",
        "privacy_status": "public",
        "upload_status": "processed"
    }
    res = client.post("/api/download/youtube", json={"video_id": "11111111111"})
    assert res.status_code == 403
    assert res.json()["detail"] == "RIGHTS_BLOCKED"

def test_download_service_deduplicates_active_download(tmp_path):
    ds = DownloadService(tmp_path)
    ds.active_downloads["dl-123"] = {
        "video_id": "11111111111",
        "status": "DOWNLOADING"
    }
    
    with patch("app.services.download_service.SourceRightsService.allows_public_acquisition", return_value=True):
        dl_id = ds.enqueue_download("11111111111", "Test")
        assert dl_id == "dl-123" # Returns existing download

def test_download_service_returns_cached_completed(tmp_path):
    ds = DownloadService(tmp_path)
    # create a completed status file
    ds.active_downloads["dl-123"] = {
        "video_id": "11111111111",
        "status": "COMPLETED",
        "job_id": "job_1"
    }
    
    with patch("app.services.download_service.SourceRightsService.allows_public_acquisition", return_value=True):
        with patch("app.services.job_store.job_store") as mock_job_store:
            mock_job_store.get.return_value = {"status": "QUEUED_ANALYSIS"}
            dl_id = ds.enqueue_download("11111111111", "Test")
            assert dl_id == "dl-123"


from app.services.youtube_source import parse_youtube_url, canonical_watch_url
from app.services.auto_edit_director import AutoEditDirector

@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=abcdefghijk',
    'https://youtube.com/watch?v=abcdefghijk&t=20&feature=shared',
    'https://m.youtube.com/watch?v=abcdefghijk&si=abc',
    'https://youtu.be/abcdefghijk?si=abc&t=20',
])
def test_canonical_url_parser(url):
    assert parse_youtube_url(url)=='abcdefghijk'
    assert canonical_watch_url(parse_youtube_url(url))=='https://www.youtube.com/watch?v=abcdefghijk'

@pytest.mark.parametrize('url', [
    'https://evil.test/watch?v=abcdefghijk', 'https://youtube.com.evil.test/watch?v=abcdefghijk',
    'https://youtube.com@evil.test/watch?v=abcdefghijk', 'https://youtube.com:123/watch?v=abcdefghijk',
    'file:///source.mp4', 'https://www.youtube.com/channel/abcdefghijk',
    'https://youtube.com/playlist?list=abcdefghijk', 'https://youtu.be/short',
    'https://youtube.com/watch?v=abcdefghijk&v=lmnopqrstuv',
    'https://youtu.be/abcdefghijk/extra',None,32,
])
def test_untrusted_url_rejected_before_acquisition(url):
    with pytest.raises(ValueError): parse_youtube_url(url)
    with patch('app.api.routes.youtube_service.get_video_rights') as rights:
        response=client.post('/api/download/youtube',json={'url':url,'auto_edit':True})
        assert response.status_code==400
        rights.assert_not_called()

@pytest.mark.parametrize('count', [None,1,2,3,5,10])
def test_url_count_handoff_and_no_trends_filter(count):
    with patch('app.api.routes.youtube_service.get_video_rights') as rights, patch('app.api.routes.download_service') as ds:
        rights.return_value={'rights_status':'UNKNOWN','title':'Minecraft Gameplay Spanish',
            'privacy_status':'public','upload_status':'processed','channel':'Creator','channel_id':'channel'}
        ds.technical_downloadability.return_value='UNVERIFIED'
        ds.enqueue_download.return_value='dl-one'
        response=client.post('/api/download/youtube',json={'url':'https://youtu.be/abcdefghijk',
            'auto_edit':True,'clip_count':count,'count_mode':'TARGET' if count else 'AUTO'})
        assert response.status_code==200
        args,kw=ds.enqueue_download.call_args
        assert args[0]=='abcdefghijk'
        settings=kw['auto_edit_settings']
        assert settings['auto_edit'] and not settings['exact_clip_count']
        assert settings['target_clip_count']==count
        assert settings['count_mode']==('TARGET' if count else 'AUTO')

@pytest.mark.parametrize('count', [0,-1,4,11,True,'5',5.5])
def test_invalid_target_does_not_start_download(count):
    with patch('app.api.routes.download_service') as ds:
        response=client.post('/api/download/youtube',json={'url':'https://youtu.be/abcdefghijk',
            'auto_edit':True,'clip_count':count,'count_mode':'TARGET'})
        assert response.status_code==422
        ds.enqueue_download.assert_not_called()
