
import os
print("[STARTUP] process starting")
print(f"[STARTUP] DATA_ROOT={os.getenv('DATA_ROOT', '/data')}")
print("[STARTUP] directories ready")
print("[STARTUP] loading application")

import os
import cv2
import torch
# Configure resource limits for Railway (8 vCPU)
os.environ["OMP_NUM_THREADS"] = "4"
os.environ["OPENBLAS_NUM_THREADS"] = "4"
os.environ["MKL_NUM_THREADS"] = "4"
cv2.setNumThreads(4)
torch.set_num_threads(4)

from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from app.api.routes import router
from app.config import config

import logging

from app.services.logging_filters import StatusFilter

@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    uvicorn_logger = logging.getLogger("uvicorn.access")
    uvicorn_logger.addFilter(StatusFilter())

    # Start PO Token Provider for YouTube downloads
    import subprocess
    import os
    import importlib.util
    print("[STARTUP] starting bgutil PO token provider")
    try:
        plugin_found = importlib.util.find_spec(
            "yt_dlp_plugins.extractor.getpot_bgutil_http") is not None
    except ImportError:
        plugin_found = False
    print("[STARTUP] PO_TOKEN_PLUGIN_DISCOVERABLE" if plugin_found
          else "[STARTUP] PO_TOKEN_PLUGIN_UNAVAILABLE")
    deno_process = None
    from pathlib import Path
    import os
    import subprocess
    import time
    
    DIR = Path(__file__).parent.parent
    provider_dir = DIR / "tools" / "bgutil-ytdlp-pot-provider" / "server"
    deno_exe = DIR / "bin" / "deno"
    deno_cache = DIR / ".cache" / "deno"
    
    if not deno_exe.exists():
        print(f"[STARTUP] PO_TOKEN_PROVIDER_UNAVAILABLE (Missing project-local deno at {deno_exe})")
    elif not provider_dir.exists():
        print("[STARTUP] PO_TOKEN_PROVIDER_NOT_INSTALLED")
    else:
        deno_cache.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["DENO_DIR"] = str(deno_cache)
        
        node_mods = provider_dir / "node_modules"
        try:
            deno_process = subprocess.Popen(
                [str(deno_exe), "run",
                 "--allow-env", "--allow-net", "--allow-sys=ca",
                 f"--allow-ffi={node_mods}",
                 f"--allow-write={deno_cache}",
                 f"--allow-read={deno_cache},{node_mods}",
                 str(provider_dir / "src" / "main.ts")],
                cwd=str(node_mods),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env
            )
        except OSError:
            print("[STARTUP] PO_TOKEN_PROVIDER_UNAVAILABLE")
            
    if deno_process:
        from app.services.youtube_acquisition import po_provider_ready
        provider_ready = False
        for _ in range(20):
            if po_provider_ready(timeout=0.25):
                provider_ready = True
                break
            if deno_process.poll() is not None:
                break
            time.sleep(0.25)
        print("[STARTUP] PO_TOKEN_PROVIDER_READY" if provider_ready
              else "[STARTUP] PO_TOKEN_PROVIDER_UNAVAILABLE")
        
    from app.services.worker import worker
    from app.services.scheduler import scheduler
    from app.services.download_service import download_service
    import logging
    try:
        download_service.reconcile_interrupted()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Interrupted acquisition reconciliation failed: type=%s", type(exc).__name__)
    from app.services.storage_lifecycle import storage_lifecycle
    try:
        storage_lifecycle.prepare_for_new_video()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Safe startup storage reconciliation failed: type=%s", type(exc).__name__)
    try:
        storage_lifecycle.reconcile_legacy_attribution()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Legacy attribution reconciliation failed: type=%s", type(exc).__name__)
    worker.start()
    scheduler.start()
    yield
    scheduler.stop()
    download_service.stop()
    worker.stop()
    if deno_process:
        deno_process.terminate()
        try:
            deno_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            deno_process.kill()

app = FastAPI(title="AI Clipper", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")

from app.api.youtube import router as youtube_router
app.include_router(youtube_router, prefix="/api/youtube", tags=["youtube"])

# Serve UI static files
ui_dir = Path(__file__).parent / "ui"
app.mount("/static", StaticFiles(directory=str(ui_dir)), name="static")

# Serve media files
app.mount("/api/media", StaticFiles(directory=str(config.OUTPUT_DIR)), name="media")


@app.get("/health")
def read_health():
    import shutil
    import os
    # Check write access
    import tempfile
    try:
        with tempfile.TemporaryFile(dir=config.DATA_ROOT) as test_file:
            test_file.write(b"ok")
            test_file.flush()
        write_ok = True
    except Exception:
        write_ok = False
        
    ffmpeg_ok = False
    try:
        config.get_ffmpeg_path()
        ffmpeg_ok = True
    except RuntimeError:
        pass
        
    ffprobe_ok = False
    try:
        config.get_ffprobe_path()
        ffprobe_ok = True
    except RuntimeError:
        pass

    return {
        "status": "ok" if write_ok else "error",
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
        "write_access": write_ok,
        "local_mode": "RAILWAY_ENVIRONMENT" not in os.environ,
        "data_root": str(config.DATA_ROOT)
    }
@app.get("/")
def read_index():
    return FileResponse(str(ui_dir / "index.html"))

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    print("[STARTUP] starting worker")
    print("[STARTUP] starting web server")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
