import pytest
from pathlib import Path
from app.services.ffmpeg_service import FFmpegService
from app.config import config

def test_long_video_proxy_policy(monkeypatch):
    test_file = config.TEMP_DIR / "smoke_test_proxy_policy.mp4"
    if not test_file.exists():
        FFmpegService.generate_smoke_test_video(test_file)
        
    dest_file = config.TEMP_DIR / "proxy_test_output.mp4"
    if dest_file.exists():
        dest_file.unlink()

    # Test 1: Short video uses standard balanced profile
    cmd_args = []
    def mock_encode(cmd, duration, cb, active, log_path):
        cmd_args.extend(cmd)
        return 0

    monkeypatch.setattr("app.services.proxy_encoder.encode_proxy_command", mock_encode)
    FFmpegService.create_proxy(test_file, dest_file, source_duration=300.0) # 5 mins
    
    assert "-r" in cmd_args
    fps_index = cmd_args.index("-r")
    assert cmd_args[fps_index + 1] == "15"
    assert "scale=-2:480,format=yuv420p" in cmd_args
    
    # Test 2: Long video uses compact profile automatically
    cmd_args.clear()
    FFmpegService.create_proxy(test_file, dest_file, source_duration=1800.0) # 30 mins
    fps_index = cmd_args.index("-r")
    assert cmd_args[fps_index + 1] == "5"
    assert "scale=-2:360,format=yuv420p" in cmd_args

    # Test 3: Speech driven flag uses compact profile even if short
    cmd_args.clear()
    FFmpegService.create_proxy(test_file, dest_file, source_duration=300.0, is_speech_driven=True)
    fps_index = cmd_args.index("-r")
    assert cmd_args[fps_index + 1] == "5"

    test_file.unlink()

