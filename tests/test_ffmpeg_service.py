import pytest
from pathlib import Path
from app.services.ffmpeg_service import FFmpegService
from app.config import config

def test_ffmpeg_installed():
    assert FFmpegService.check_installed() == True

def test_smoke_video_generation_and_metadata():
    test_file = config.TEMP_DIR / "smoke_test.mp4"
    
    # Generate smoke video
    success = FFmpegService.generate_smoke_test_video(test_file)
    assert success == True
    assert test_file.exists()
    
    # Test metadata parsing
    metadata = FFmpegService.get_metadata(test_file)
    assert metadata.filename == "smoke_test.mp4"
    assert 4.9 <= metadata.duration <= 5.1
    assert metadata.width == 1280
    assert metadata.height == 720
    assert "h264" in metadata.video_codec.lower()
    
    # Test cutting/rendering
    cut_file = config.TEMP_DIR / "smoke_cut.mp4"
    mock_candidate = {
        "source_segments": [{"start_seconds": 1.0, "end_seconds": 3.0, "reason": "test"}],
        "face_tracks": []
    }
    success = FFmpegService.render_clip(
        source=test_file,
        dest=cut_file,
        candidate_data=mock_candidate,
        aspect_mode="center_crop",
        output_resolution="1080x1920"
    )
    
    assert success == True
    assert cut_file.exists()
    
    cut_meta = FFmpegService.get_metadata(cut_file)
    assert 1.9 <= cut_meta.duration <= 2.1
    assert cut_meta.width == 1080
    assert cut_meta.height == 1920
    
    # Cleanup
    test_file.unlink()
    cut_file.unlink()

