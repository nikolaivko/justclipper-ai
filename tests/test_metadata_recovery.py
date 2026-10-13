import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services import clip_metadata_service as service
from app.services.youtube_metadata import build_youtube_metadata, is_placeholder_title
from app.services.gemini_service import GeminiService


def excerpt():
    return {"rank": 1, "title": "Source excerpt 06:40–07:00",
            "short_description": "Timed excerpt from the original video. Review its content before rendering.",
            "topic": "SOURCE_EXCERPT", "content_type": "SOURCE_EXCERPT",
            "categories": ["TIMELINE_EXCERPT"]}


def test_placeholder_metadata_never_contains_internal_hashtags():
    data = build_youtube_metadata({}, {"rank": 1}, excerpt())
    assert is_placeholder_title(data["title"])
    assert data["hashtags"] == ["#Shorts", "#YouTubeShorts"]
    assert not data["tags"]
    assert "Review its content" not in data["description"]


def test_recovers_title_from_actual_render_and_preserves_attribution(monkeypatch):
    details = {"title": "Он поднял штангу одной рукой", "short_description": "Атлет поднимает штангу одной рукой.", "topic": "Тренировка"}
    describe = Mock(return_value=details)
    monkeypatch.setattr(service, 'describe_clip', describe)
    job = {"id": "test", "source_id": "youtube_abcdefghijk", "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
           "source_video_id": "abcdefghijk", "source_video_title": "Gym session",
           "source_channel_name": "Original creator", "rights_status": "LICENSED"}
    render = {"rank": 1}
    service.ensure_clip_metadata(job, render, Path('/tmp/actual-short.mp4'), excerpt())
    describe.assert_called_once_with(Path('/tmp/actual-short.mp4'))
    assert render['metadata_status'] == 'GENERATED'
    metadata = render['youtube_metadata']
    assert metadata['title'] == details['title']
    assert 'Copyright & Source Notice:' in metadata['description']
    assert job['source_url'] in metadata['description']
    assert not any('Excerpt' in tag for tag in metadata['hashtags'])


def test_title_failure_keeps_video_successful_and_requests_review(monkeypatch):
    monkeypatch.setattr(service, 'describe_clip', Mock(side_effect=RuntimeError('provider unavailable')))
    render = {"rank": 1, "success": True}
    service.ensure_clip_metadata({}, render, Path('/tmp/render.mp4'), excerpt())
    assert render['success'] is True
    assert render['metadata_status'] == 'REVIEW_REQUIRED'
    assert render['metadata_error'] == 'TITLE_GENERATION_FAILED'


def test_semantic_title_reuses_metadata_without_extra_api_cost(monkeypatch):
    describe = Mock()
    monkeypatch.setattr(service, 'describe_clip', describe)
    render = {'rank': 1, 'youtube_metadata': {'title': 'He lifts the bar with one hand'}}
    service.ensure_clip_metadata({}, render, Path('/tmp/render.mp4'))
    describe.assert_not_called()


def test_generation_rejects_placeholder_and_cleans_remote_file(monkeypatch):
    monkeypatch.setattr(service.config, 'gemini_models', lambda preferred: ['gemini-3.5-flash-lite', 'gemini-3.8-flash'])
    video = SimpleNamespace(name='files/render')
    generate = Mock(side_effect=[
        SimpleNamespace(text=json.dumps({'title': 'Source excerpt 00:00–00:20', 'short_description': 'A gym clip is shown.', 'topic': 'Gym'})),
        SimpleNamespace(text=json.dumps({'title': 'He lifts the bar with one hand', 'short_description': 'An athlete lifts a bar with one hand.', 'topic': 'Gym'}))])
    fake = SimpleNamespace(model_name='gemini-3.8-flash', upload_video=Mock(return_value=video),
                           delete_video=Mock(), client=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(service, 'GeminiService', lambda: fake)
    result = service.describe_clip(Path('/tmp/short.mp4'))
    assert result['title'] == 'He lifts the bar with one hand'
    assert generate.call_count == 2
    assert generate.call_args.kwargs['contents'][0] is video
    fake.delete_video.assert_called_once_with('files/render')


def test_remote_processing_timeout_cleans_uploaded_file(monkeypatch):
    gemini = GeminiService()
    remote = SimpleNamespace(name='files/stuck', state='PROCESSING')
    gemini.client = SimpleNamespace(files=SimpleNamespace(upload=Mock(return_value=remote), delete=Mock()))
    monkeypatch.setattr('app.services.gemini_service.time.monotonic', Mock(side_effect=[0, 181]))
    with patch("app.services.gemini_service.Path.exists", return_value=True), patch("app.services.gemini_service.Path.is_file", return_value=True), patch("app.services.gemini_service.Path.stat") as mock_stat:
        mock_stat.return_value.st_size = 1000
        with pytest.raises(TimeoutError):
            gemini.upload_video(Path("/tmp/short.mp4"))
    gemini.client.files.delete.assert_called_once_with(name='files/stuck')


def test_failed_regeneration_keeps_a_reviewed_title(monkeypatch):
    monkeypatch.setattr(service, 'describe_clip', Mock(side_effect=RuntimeError('unavailable')))
    render = {'rank': 1, 'metadata_status': 'REVIEWED',
              'youtube_metadata': {'title': 'He lifts the bar with one hand'}}
    service.ensure_clip_metadata({}, render, Path('/tmp/render.mp4'), force=True)
    assert render['metadata_status'] == 'REVIEWED'
    assert render['youtube_metadata']['title'] == 'He lifts the bar with one hand'
    assert render['metadata_error'] == 'TITLE_GENERATION_FAILED'


def test_provider_overload_has_bounded_budget_and_safe_error(monkeypatch):
    monkeypatch.setattr(service.config, "gemini_models", lambda preferred: ["gemini-3.5-flash-lite", "gemini-3.8-flash"])
    class Overloaded(Exception):
        code = 503
    generate = Mock(side_effect=Overloaded('private provider details'))
    fake = SimpleNamespace(model_name='gemini-3.8-flash', upload_video=Mock(return_value=SimpleNamespace(name='files/render')),
                           delete_video=Mock(), client=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(service, 'GeminiService', lambda: fake)
    sleep = Mock()
    monkeypatch.setattr('app.services.gemini_requests.time.sleep', sleep)
    render = {'rank': 1, 'youtube_metadata': {'title': 'Source excerpt 00:00–00:20', 'hashtags': ['#Source_Excerpt']}}
    service.ensure_clip_metadata({}, render, Path('/tmp/short.mp4'), excerpt())
    assert generate.call_count == 6  # Three attempts per verified model.
    assert sleep.call_count == 4  # No sleep after either final attempt.
    fake.delete_video.assert_called_once()
    assert render['metadata_error'] == 'GEMINI_UNAVAILABLE'
    assert render['youtube_metadata']['hashtags'] == ['#Shorts', '#YouTubeShorts']
