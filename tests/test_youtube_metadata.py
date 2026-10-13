from app.services.youtube_metadata import build_youtube_metadata


def test_render_persists_distinct_metadata_without_publishing(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from unittest.mock import Mock
    from app.config import config
    from app.models.schemas import ClipCandidate, RenderSettings
    from app.services.clip_service import ClipService
    from app.services.ffmpeg_service import FFmpegService
    from app.services.job_store import job_store
    from app.services.video_analyzer import LocalVideoAnalyzer
    from app.services.visual_director import VisualDirector
    from app.services.youtube_publisher import youtube_publisher

    jobs, output = tmp_path / "jobs", tmp_path / "output"
    jobs.mkdir()
    output.mkdir()
    monkeypatch.setattr(job_store, "jobs_dir", jobs)
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "color=c=blue:s=32x32:r=10", "-f", "lavfi", "-i",
        "anullsrc=r=44100:cl=mono", "-t", "0.4", "-c:v", "mpeg4",
        "-c:a", "aac", "-shortest", str(source)], check=True, capture_output=True)
    job = ClipService.create_job("metadatajob", "source.mp4", source)
    job.update(source_id="youtube_abcdefghijk", source_video_id="abcdefghijk",
               source_video_title="Family Visit", source_channel_name="Family Studio",
               source_channel_id="UCfamily", source_url="https://evil.example/watch",
               rights_status="UNKNOWN", rights_evidence="STANDARD_YOUTUBE_LICENSE")
    job_store.set(job["id"], job)
    candidates = [ClipCandidate(
        rank=rank, title=title, short_description=description,
        topic=topic, content_type="VLOG", reason="Standalone moment",
        categories=["FUNNY", "REACTION"], context_dependency="NONE",
        source_segments=[{"start_seconds": 0.0, "end_seconds": 0.3}], payoff_present=True)
        for rank, title, description, topic in (
            (1, "Grandpa Laughs at the Surprise", "Grandpa laughs when the family surprises him.", "Birthday"),
            (2, "The Family Reveals the Cake", "The family brings out a birthday cake.", "Birthday cake"))]
    monkeypatch.setattr(LocalVideoAnalyzer, "track_faces", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(LocalVideoAnalyzer, "detect_scenes", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(LocalVideoAnalyzer, "detect_speech", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(VisualDirector, "build_shot_plan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(FFmpegService, "render_clip", lambda source, dest, **_kwargs:
                            shutil.copy(source, dest) is not None)
    monkeypatch.setattr("app.services.render_qc.check_render",
                        lambda *_args, **_kwargs: None)
    upload = Mock()
    monkeypatch.setattr(youtube_publisher, "publish_video", upload)

    ClipService.process_renders("metadatajob", candidates, RenderSettings(subtitles=False))
    persisted = job_store.get("metadatajob")
    assert persisted["status"] == "COMPLETED"
    assert [r["youtube_metadata"]["title"] for r in persisted["renders"]] == [
        candidate.title for candidate in candidates]
    assert persisted["renders"][0]["youtube_metadata"]["description"] != \
        persisted["renders"][1]["youtube_metadata"]["description"]
    for render in persisted["renders"]:
        assert render["source_video_id"] == "abcdefghijk"
        assert render["source_video_title"] == "Family Visit"
        assert render["source_channel_name"] == "Family Studio"
        assert render["source_channel_id"] == "UCfamily"
        assert render["rights_status"] == "UNKNOWN"
        assert render["rights_evidence"] == "STANDARD_YOUTUBE_LICENSE"
        assert render["source_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
        assert render["youtube_metadata"]["source_url"] == render["source_url"]
        assert "⚠️ Copyright & Source Notice:" in render["youtube_metadata"]["description"]
    upload.assert_not_called()


def test_per_short_metadata_uses_only_known_content():
    job = {
        "source_id": "youtube_abcdefghijk",
        "source_video_id": "abcdefghijk",
        "source_video_title": "A Family Visit",
        "source_channel_name": "Family Studio",
        "source_channel_id": "UCfamily",
        "rights_status": "UNKNOWN",
        "rights_evidence": "STANDARD_YOUTUBE_LICENSE",
        "candidates": [{
            "rank": 1,
            "title": "Grandpa Laughs at the Birthday Surprise",
            "short_description": "The family surprises grandpa with a cake, and he starts laughing.",
            "topic": "Birthday surprise",
            "content_type": "VLOG",
            "categories": ["FUNNY", "REACTION", "WHOLESOME"],
            "story_arc": {"hook": "Grandpa spots the cake", "setup": "Family gathers",
                          "payoff": "He laughs"},
        }],
    }
    metadata = build_youtube_metadata(job, {"rank": 1, "title": "Grandpa Laughs at the Birthday Surprise"})
    assert metadata["title"] == "Grandpa Laughs at the Birthday Surprise"
    assert metadata["description"].startswith("The family surprises grandpa")
    assert "🎥 Original video: A Family Visit" in metadata["description"]
    assert "📺 Original creator/channel: Family Studio" in metadata["description"]
    assert "🔗 Source: https://www.youtube.com/watch?v=abcdefghijk" in metadata["description"]
    assert "⚠️ Copyright & Source Notice:" in metadata["description"]
    assert metadata["source_video_id"] == "abcdefghijk"
    assert metadata["source_video_title"] == "A Family Visit"
    assert metadata["source_channel_id"] == "UCfamily"
    assert metadata["rights_status"] == "UNKNOWN"
    assert "✂️ Edited with justClipper" in metadata["description"]
    assert "fair use" not in metadata["description"].lower()
    assert "music" not in metadata["description"].lower()
    assert 8 <= len(metadata["hashtags"]) <= 15
    assert len(metadata["hashtags"]) == len(set(tag.casefold() for tag in metadata["hashtags"]))
    assert all(tag.startswith("#") for tag in metadata["hashtags"])
    assert all(not tag.startswith("#") for tag in metadata["tags"])
    assert len(metadata["title"]) <= 100
    assert len(metadata["description"]) <= 5000
    assert sum(len(tag) + 1 for tag in metadata["tags"]) <= 500


def test_unknown_channel_and_source_are_omitted():
    job = {"source_url": "https://evil.example/not-a-youtube-source",
           "candidates": [{"rank": 2, "title": "A surprising reaction",
                            "short_description": "A guest reacts to a surprise.",
                            "topic": "surprise", "content_type": "VLOG",
                            "categories": ["REACTION"]}]}
    metadata = build_youtube_metadata(job, {"rank": 2, "title": "A surprising reaction"})
    assert "Creator/Channel" not in metadata["description"]
    assert "🎥 Original video:" not in metadata["description"]
    assert "Copyright & Source Notice" not in metadata["description"]
    assert "evil.example" not in metadata["description"]
    assert metadata["source_channel"] is None


def test_unknown_youtube_channel_is_omitted_but_known_source_retained():
    job = {"source_id": "youtube_abcdefghijk", "source_video_title": "Known source",
           "candidates": [{"rank": 1, "title": "A short", "short_description": "A clear moment.",
                           "topic": "moment", "content_type": "STORY", "categories": []}]}
    metadata = build_youtube_metadata(job, {"rank": 1, "title": "A short"})
    description = metadata["description"]
    assert "🎥 Original video: Known source" in description
    assert "https://www.youtube.com/watch?v=abcdefghijk" in description
    assert "Original creator/channel:" not in description
    assert "published by " not in description
    assert "published on YouTube" in description
    assert metadata["source_channel_name"] is None


def test_untrusted_source_url_and_invalid_id_are_not_published():
    from app.services.youtube_source import source_attribution
    good = source_attribution({"source_id": "youtube_abcdefghijk",
                               "source_url": "https://evil.example/private"})
    assert good["source_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert source_attribution({"source_id": "youtube_../private"}) == {}
    assert source_attribution({"source_video_id": "abcdefghijk"}) == {}
    assert source_attribution({"source_id": "youtube_abcdefghijk",
                               "source_video_title": "", "source_title": "Unverified UI title"})["source_video_title"] == ""
    assert source_attribution({"source_id": "youtube_abcdefghijk",
                               "source_video_id": "lmnopqrstuv"}) == {}
