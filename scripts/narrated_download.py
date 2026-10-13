"""Isolated narrated download child; uses Clipper dependencies, not its worker."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.youtube_acquisition import acquire_public_video, AcquisitionFailure

def main():
    folder = Path(sys.argv[1]).resolve()
    expected = ROOT/'tools/narrated-dev/data/narrated/jobs'
    if folder.parent != expected or not folder.is_dir():
        raise ValueError('Invalid narrated project folder')
    request = json.loads((folder/'request.json').read_text())
    temp = folder/'acquisition'
    temp.mkdir(exist_ok=True)
    last = 0
    def status(stage, progress, message=''):
        out = folder/'status.new'
        out.write_text(json.dumps(dict(stage=stage, progress=progress, message=message, updated=time.time())))
        out.replace(folder/'status.json')
    def progress(event):
        nonlocal last
        size = sum(p.stat().st_size for p in temp.rglob('*') if p.is_file())
        if size > 8*1024**3 or shutil.disk_usage(folder).free < 1024**3:
            raise RuntimeError('Download exceeds the 8 GB limit or available disk space')
        now = time.monotonic()
        if now-last < 1: return
        last = now
        total = event.get('total_bytes') or event.get('total_bytes_estimate')
        downloaded = event.get('downloaded_bytes', 0)
        percent = min(100, downloaded/total*100) if total else 0
        status('Downloading YouTube video', percent,
               f"Current stream: {percent:.0f}% · {downloaded/1024**2:.0f} MB" if total else f"Downloaded {downloaded/1024**2:.0f} MB")
    try:
        candidate, info, strategy = acquire_public_video(
            request['video_id'], temp, folder/'download-cache',
            lambda p: p.is_file() and p.stat().st_size > 0,
            lambda s: status('Connecting to YouTube', 0), progress,
            download_limits=(10800, 8*1024**3))
        candidate.replace(folder/'source.mp4')
        status('Download complete', 100)
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, AcquisitionFailure) else 'DOWNLOAD_FAILED'
        message = ('YouTube blocked this download. Refresh the Safari session in Clipper or upload a file.'
                   if code == 'BOT_PROTECTION' else f'Download failed ({code}). Check disk space, video duration, or upload the source file.')
        status('failed', 0, message)
        return 1
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(folder/'download-cache', ignore_errors=True)

if __name__ == '__main__':
    raise SystemExit(main())
