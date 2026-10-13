import json
import time
from unittest.mock import Mock

from app.config import config
from app.services.source_rights_service import SourceRightsService
from app.services.source_store import SourceStore
from app.services.youtube_service import YouTubeService


def test_rights_evidence_and_acquisition_policy():
    assess = SourceRightsService.assess_rights
    assert assess({"status": {"license": "youtube", "privacyStatus": "public"}}) == {
        "rights_status": "UNKNOWN", "rights_reason": "STANDARD_YOUTUBE_LICENSE"}
    assert assess({"status": {"privacyStatus": "public"}}) == {
        "rights_status": "UNKNOWN", "rights_reason": "LICENSE_NOT_PROVIDED"}
    assert assess({"status": {"license": "other"}}) == {
        "rights_status": "UNKNOWN", "rights_reason": "UNRECOGNIZED_LICENSE"}
    assert assess({"status": {"license": "creativeCommon", "privacyStatus": "public"}}) == {
        "rights_status": "LICENSED", "rights_reason": "YOUTUBE_CREATIVE_COMMONS"}
    assert assess({"status": {"license": "creativeCommon", "privacyStatus": "private"}}) == {
        "rights_status": "RESTRICTED", "rights_reason": "PRIVATE_VIDEO"}
    assert assess({"status": {"license": "youtube", "privacyStatus": "public"},
                   "snippet": {"channelId": "owner"}}, owned_channel_id="owner") == {
        "rights_status": "OWNED", "rights_reason": "CONFIGURED_CREATOR_CHANNEL"}
    assert assess({"status": {"license": "youtube", "privacyStatus": "public"},
                   "snippet": {"channelId": "other"}}, owned_channel_id="owner")["rights_status"] == "UNKNOWN"
    for state in ("OWNED", "LICENSED", "PERMITTED"):
        assert SourceRightsService.allows_auto_acquisition(state)
    for state in ("UNKNOWN", "RESTRICTED", "CREATIVE_COMMONS_OR_COMPATIBLE", ""):
        assert not SourceRightsService.allows_auto_acquisition(state)


def test_old_trends_cache_is_refreshed_and_rights_reason_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(config, "YOUTUBE_OWNED_CHANNEL_ID", "")
    service = YouTubeService(tmp_path)
    service.cache_file.write_text(json.dumps({
        "timestamp": time.time(), "trends": [{"id": "abcdefghijk", "rights_status": "RESTRICTED"}]}))
    client = Mock()
    client.search().list().execute.return_value = {"items": []}
    client.videos().list().execute.return_value = {"items": [{
        "id": "abcdefghijk", "status": {"license": "youtube", "privacyStatus": "public",
                                      "uploadStatus": "processed"},
        "snippet": {"categoryId": "22", "title": "the example and test", "channelId": "someone-else",
                    "channelTitle": "Example creator"},
        "contentDetails": {"duration": "PT5M"},
        "statistics": {"viewCount": "1"}}]}
    monkeypatch.setattr(service, "_get_client", lambda: client)
    trends, _ = service.get_us_trends()
    assert trends[0]["rights_status"] == "UNKNOWN"
    assert trends[0]["rights_reason"] == "STANDARD_YOUTUBE_LICENSE"
    assert trends[0]["rights_evidence"] == "STANDARD_YOUTUBE_LICENSE"
    assert trends[0]["channel_id"] == "someone-else"
    assert trends[0]["privacy_status"] == "public"
    assert trends[0]["upload_status"] == "processed"
    snapshot = json.loads((service.cache_dir / "creator-v1/latest.json").read_text())["id"]
    cached = json.loads((service.cache_dir / f"creator-v1/feed-{snapshot}.json").read_text())
    assert cached["rights_cache_version"] == service.RIGHTS_CACHE_VERSION
    assert service.get_us_trends() == (trends, None)
    assert client.videos().list().execute.call_count == 1


def test_legacy_verified_source_state_is_normalized():
    record = {
        "source_id": "youtube_abcdefghijk", "provider": "youtube",
        "provider_video_id": "abcdefghijk", "title": "the example and test", "source_path": "/tmp/example.mp4",
        "duration": 1, "width": 100, "height": 100, "video_codec": "h264",
        "audio_codec": "aac", "rights_status": "CREATIVE_COMMONS_OR_COMPATIBLE",
        "acquisition_status": "COMPLETED", "processing_status": "READY", "created_at": 1,
    }
    asset = SourceStore._asset_from_record(record)
    assert asset.rights_status == "LICENSED"
    assert record["rights_status"] == "CREATIVE_COMMONS_OR_COMPATIBLE"
