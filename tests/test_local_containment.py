import os
from pathlib import Path
from app.config import config

def test_project_root_resolution():
    assert config.PROJECT_ROOT.name == "justclipper"
    assert config.PROJECT_ROOT.is_absolute()

def test_data_root_containment():
    assert config.PROJECT_ROOT in config.DATA_ROOT.parents

def test_cache_model_containment():
    assert config.PROJECT_ROOT in config.DATA_MODELS.parents or config.DATA_MODELS == config.PROJECT_ROOT / ".models"
    assert config.PROJECT_ROOT in config.DATA_CACHE.parents or config.DATA_CACHE == config.PROJECT_ROOT / ".cache"

def test_temp_containment():
    assert config.PROJECT_ROOT in config.DATA_TEMP.parents

def test_sources_containment():
    assert config.PROJECT_ROOT in config.DATA_SOURCES.parents

def test_oauth_token_containment():
    assert config.PROJECT_ROOT in config.DATA_YOUTUBE.parents

def test_ffmpeg_available():
    assert config.get_ffmpeg_path() is not None
    assert config.get_ffprobe_path() is not None


def test_media_tools_ignore_external_system_path(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(type(config), 'PROJECT_ROOT', tmp_path)
    monkeypatch.delenv('FFMPEG_PATH', raising=False)
    monkeypatch.delenv('FFPROBE_PATH', raising=False)
    for name in ('ffmpeg', 'ffprobe'):
        path = tmp_path / 'bin' / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    monkeypatch.setenv('PATH', '/opt/homebrew/bin:/usr/local/bin:/usr/bin')
    assert config.get_ffmpeg_path() == str(tmp_path / 'bin/ffmpeg')
    assert config.get_ffprobe_path() == str(tmp_path / 'bin/ffprobe')


def test_external_override_and_symlink_are_rejected(tmp_path, monkeypatch):
    import pytest
    import sys
    monkeypatch.setattr(sys, 'platform', 'darwin')
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setattr(type(config), 'PROJECT_ROOT', root)
    # A sibling whose prefix matches is still outside the project.
    external = tmp_path / 'project-other' / 'ffmpeg'
    external.parent.mkdir()
    external.write_text('#!/bin/sh\nexit 0\n')
    external.chmod(0o755)
    monkeypatch.setenv('FFMPEG_PATH', str(external))
    with pytest.raises(RuntimeError, match='inside PROJECT_ROOT'):
        config.get_ffmpeg_path()
    monkeypatch.delenv('FFMPEG_PATH')
    (root / 'bin').mkdir()
    (root / 'bin/ffmpeg').symlink_to(external)
    with pytest.raises(RuntimeError, match='inside PROJECT_ROOT'):
        config.get_ffmpeg_path()


def test_missing_local_binary_does_not_fall_back(tmp_path, monkeypatch):
    import pytest
    import sys
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(type(config), 'PROJECT_ROOT', tmp_path)
    monkeypatch.delenv('FFPROBE_PATH', raising=False)
    with pytest.raises(RuntimeError, match='Project-local ffprobe'):
        config.get_ffprobe_path()
