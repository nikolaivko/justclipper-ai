import os
import shutil
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    CREATOR_SEED_HANDLES = tuple(handle.strip() for handle in os.getenv(
        "CREATOR_SEED_HANDLES", "@MrBeast,@KaiCenat,@IShowSpeed").split(",") if handle.strip())
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    GEMINI_DEEP_MODEL = os.getenv("GEMINI_DEEP_MODEL", "").strip()
    GEMINI_METADATA_MODEL = os.getenv("GEMINI_METADATA_MODEL", "").strip()
    GEMINI_FALLBACK_MODELS = tuple(m.strip() for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.8-flash"
    ).split(",") if m.strip())
    AUTO_EDIT_MAX_CANDIDATES = max(1, min(12, int(os.getenv("AUTO_EDIT_MAX_CANDIDATES", "8"))))
    AUTO_EDIT_DEEP_CANDIDATES = max(1, min(8, int(os.getenv("AUTO_EDIT_DEEP_CANDIDATES", "5"))))

    # Catalog + official multimodal documentation verified 2026-09-30.
    # New IDs must be catalog/capability verified before adding here.
    VERIFIED_GEMINI_MODELS = frozenset({
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", 
        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-pro-preview"})

    STANDARD_MODEL_POOL = [
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"
    ]
    LIGHTWEIGHT_MODEL_POOL = [
        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"
    ]

    @classmethod
    def gemini_models(cls, preferred=None, task="standard"):
        """Return deterministic fallback list based on task."""
        if task == "lightweight":
            pool = cls.LIGHTWEIGHT_MODEL_POOL + cls.STANDARD_MODEL_POOL
        else:
            pool = cls.STANDARD_MODEL_POOL + cls.LIGHTWEIGHT_MODEL_POOL
            
        configured = [preferred] if preferred else []
        configured.extend([cls.GEMINI_MODEL] + list(cls.GEMINI_FALLBACK_MODELS) + pool)
        models = list(dict.fromkeys(m for m in configured if m in cls.VERIFIED_GEMINI_MODELS))
        if not models:
            raise ValueError("No verified Gemini video models configured")
        return models

    YOUTUBE_DATA_API_KEY = os.getenv("YOUTUBE_DATA_API_KEY")
    YOUTUBE_OWNED_CHANNEL_ID = os.getenv("YOUTUBE_OWNED_CHANNEL_ID", "").strip()
    YOUTUBE_DEFAULT_PRIVACY = "public"
    
    # Core roots
    PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parent.parent))
    DATA_ROOT = Path(os.getenv("DATA_ROOT", PROJECT_ROOT / "data"))
    APP_ROOT = Path(os.getenv("APP_ROOT", PROJECT_ROOT))

    # The macOS application owns only files under its checkout. Validate before
    # mkdir/imports can create a cache in an environment-provided external path.
    if sys.platform == "darwin":
        _checkout = Path(__file__).resolve().parent.parent
        if PROJECT_ROOT.resolve() != _checkout:
            raise RuntimeError("PROJECT_ROOT must be the justClipper checkout")
        for _path in (DATA_ROOT, APP_ROOT):
            if not _path.resolve().is_relative_to(_checkout):
                raise RuntimeError("justClipper paths must remain inside PROJECT_ROOT")
    
    # Subdirectories
    DATA_MODELS = PROJECT_ROOT / ".models"
    DATA_CACHE = PROJECT_ROOT / ".cache"
    DATA_TEMP = DATA_ROOT / "temp"
    DATA_CONFIG = DATA_ROOT / "config"
    DATA_LOCAL = DATA_ROOT / "local"
    
    DATA_OUTPUT = DATA_ROOT / "output"
    DATA_INPUT = DATA_ROOT / "input"
    DATA_SOURCES = DATA_ROOT / "sources"
    DATA_JOBS = DATA_ROOT / "jobs"
    DATA_THUMBNAILS = DATA_ROOT / "thumbnails"
    DATA_DB = DATA_ROOT / "database"
    DATA_YOUTUBE = DATA_ROOT / "youtube"
    
    # Logs and Tools
    PROJECT_LOGS = PROJECT_ROOT / "logs"
    PROJECT_TOOLS = PROJECT_ROOT / "tools"

    # Export paths that other services can consume natively
    PROJECT_MODELS = DATA_MODELS
    PROJECT_CACHE = DATA_CACHE
    PROJECT_TEMP = DATA_TEMP
    INPUT_DIR = DATA_INPUT
    OUTPUT_DIR = DATA_OUTPUT
    TEMP_DIR = DATA_TEMP

    # Autonomous configuration
    AUTONOMOUS_TRENDS_ENABLED = os.getenv("AUTONOMOUS_TRENDS_ENABLED", "false").lower() == "true"
    AUTO_PROCESS_ELIGIBLE_SOURCES = os.getenv("AUTO_PROCESS_ELIGIBLE_SOURCES", "false").lower() == "true"
    TREND_REGION = os.getenv("TREND_REGION", "US")
    TREND_SCAN_INTERVAL_MINUTES = int(os.getenv("TREND_SCAN_INTERVAL_MINUTES", "60"))
    MAX_HEAVY_JOBS = int(os.getenv("MAX_HEAVY_JOBS", "1"))
    MIN_FREE_DISK_GB = float(os.getenv("MIN_FREE_DISK_GB", "5.0"))
    DIRECT_UPLOAD_MIN_FREE_GB = float(os.getenv("DIRECT_UPLOAD_MIN_FREE_GB", "2.0"))
    ACQUISITION_WORKING_RESERVE_GB = float(os.getenv("ACQUISITION_WORKING_RESERVE_GB", "0.75"))
    UPLOAD_WORKING_RESERVE_GB = float(os.getenv("UPLOAD_WORKING_RESERVE_GB", "0.125"))
    EDITORIAL_MIN_SCORE = float(os.getenv("EDITORIAL_MIN_SCORE", "45"))
    EFFECT_MIN_CONFIDENCE = float(os.getenv("EFFECT_MIN_CONFIDENCE", "0.78"))
    EDITORIAL_SCORE_WEIGHTS = os.getenv("EDITORIAL_SCORE_WEIGHTS", "")
    FINAL_OUTPUT_RETENTION_HOURS = float(os.getenv("FINAL_OUTPUT_RETENTION_HOURS", "72"))
    FAILED_JOB_RETENTION_HOURS = float(os.getenv("FAILED_JOB_RETENTION_HOURS", "24"))
    
    # Environment strict overrides for 3rd party tools
    os.environ["HF_HOME"] = str(DATA_CACHE / "huggingface")
    os.environ["TRANSFORMERS_CACHE"] = str(DATA_CACHE / "huggingface")
    os.environ["TORCH_HOME"] = str(DATA_CACHE / "torch")
    os.environ["XDG_CACHE_HOME"] = str(DATA_CACHE)
    os.environ["XDG_CONFIG_HOME"] = str(DATA_CONFIG)
    os.environ["XDG_DATA_HOME"] = str(DATA_LOCAL / "share")
    os.environ["ULTRALYTICS_CONFIG_DIR"] = str(DATA_CONFIG / "ultralytics")
    os.environ["HOME_FOR_TOOLS"] = str(DATA_ROOT / ".home")
    os.environ["TMPDIR"] = str(DATA_TEMP)
    os.environ["TMP"] = str(DATA_TEMP)
    os.environ["TEMP"] = str(DATA_TEMP)
    os.environ["PIP_CACHE_DIR"] = str(DATA_CACHE / "pip")
    os.environ["DENO_DIR"] = str(DATA_CACHE / "deno")
    
    # Create all required persistent directories
    for d in [DATA_ROOT, DATA_MODELS, DATA_CACHE, DATA_TEMP, DATA_CONFIG, DATA_LOCAL, DATA_OUTPUT, DATA_INPUT, DATA_SOURCES, DATA_JOBS, DATA_THUMBNAILS, DATA_DB, DATA_YOUTUBE, PROJECT_LOGS, PROJECT_TOOLS]:
        if sys.platform == "darwin" and not d.resolve().is_relative_to(PROJECT_ROOT.resolve()):
            raise RuntimeError("justClipper storage symlink escapes PROJECT_ROOT")
        d.mkdir(parents=True, exist_ok=True)
        
    @classmethod
    def _local_media_tool(cls, name: str) -> str:
        path = Path(os.getenv(f"{name.upper()}_PATH", cls.PROJECT_ROOT / "bin" / name))
        if not path.is_absolute():
            path = cls.PROJECT_ROOT / path
        path = path.resolve()
        if not path.is_relative_to(cls.PROJECT_ROOT.resolve()):
            raise RuntimeError(f"{name} must be inside PROJECT_ROOT")
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"Project-local {name} executable not found")
        return str(path)

    @classmethod
    def get_ffmpeg_path(cls) -> str:
        if sys.platform == "darwin":
            return cls._local_media_tool("ffmpeg")
        # 1. Env override
        if "FFMPEG_PATH" in os.environ:
            return os.environ["FFMPEG_PATH"]
        # Container deployments retain their existing tool resolution.
        sys_ffmpeg = shutil.which("ffmpeg")
        if sys_ffmpeg:
            return sys_ffmpeg
        raise RuntimeError("FFmpeg executable not found.")
        
    @classmethod
    def get_ffprobe_path(cls) -> str:
        if sys.platform == "darwin":
            return cls._local_media_tool("ffprobe")
        # 1. Env override
        if "FFPROBE_PATH" in os.environ:
            return os.environ["FFPROBE_PATH"]
        sys_ffprobe = shutil.which("ffprobe")
        if sys_ffprobe:
            return sys_ffprobe
        raise RuntimeError("FFprobe executable not found.")

config = Config()
