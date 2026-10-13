import os
import hashlib
import json
from pathlib import Path
from typing import List, Dict
import logging
from app.config import config


logger = logging.getLogger(__name__)

class TranscriptionService:
    _model = None

    @staticmethod
    def group_words(segments, source_offset: float = 0.0,
                    pacing_mode: str = "CONVERSATIONAL") -> List[Dict]:
        """Phrase captions from selected-audio word times, preserving word boundaries."""
        captions = []
        max_words = (4 if pacing_mode in {"HYPER", "FAST", "ENERGETIC"}
                     else 6 if pacing_mode in {"STORY", "EMOTIONAL", "EDUCATIONAL"}
                     else 6)
        for segment in segments:
            group = []
            for word in segment.words or []:
                token = word.word.strip()
                if not token:
                    continue
                if group and word.start - group[-1].end >= 0.45:
                    captions.append({"start_seconds": source_offset + group[0].start,
                                     "end_seconds": source_offset + group[-1].end,
                                     "text": " ".join(item.word.strip() for item in group)})
                    group = []
                group.append(word)
                phrase_end = token.endswith((".", "?", "!", ";", ":"))
                clause_end = token.endswith(",") and len(group) >= 2
                if phrase_end or clause_end or len(group) >= max_words:
                    captions.append({"start_seconds": source_offset + group[0].start,
                                     "end_seconds": source_offset + group[-1].end,
                                     "text": " ".join(item.word.strip() for item in group)})
                    group = []
            if group:
                captions.append({"start_seconds": source_offset + group[0].start,
                                 "end_seconds": source_offset + group[-1].end,
                                 "text": " ".join(item.word.strip() for item in group)})
        return captions

    @classmethod
    def get_model(cls):
        if cls._model is None:
            model_size = os.getenv("WHISPER_MODEL", "small")
            device = os.getenv("WHISPER_DEVICE", "cpu")
            compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
            
            # Use a local cache directory for the model
            cache_dir = config.PROJECT_MODELS / "whisper"
            cache_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"Loading faster-whisper model '{model_size}' on {device} ({compute_type})...")
            # This will automatically download and cache the model if it's not present
            from faster_whisper import WhisperModel
            cls._model = WhisperModel(
                model_size, 
                device=device, 
                compute_type=compute_type,
                download_root=str(cache_dir)
            )
            logger.info("Whisper model loaded successfully.")
            
        return cls._model

    @classmethod
    def release_model(cls):
        cls._model = None
        import gc
        gc.collect()

    @classmethod
    def transcribe_segment(cls, video_path: Path, start: float, end: float,
                           pacing_mode: str = "CONVERSATIONAL") -> List[Dict]:
        """Extracts audio for a specific segment, transcribes it, and remaps timestamps to source."""
        cache_dir = config.PROJECT_CACHE / "transcriptions"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        file_stat = video_path.stat()
        segment_id = (f"phrase-v4_{video_path.name}_{file_stat.st_size}_{file_stat.st_mtime_ns}_"
                      f"{start}_{end}_{pacing_mode}")
        cache_key = hashlib.md5(segment_id.encode()).hexdigest()
        cache_file = cache_dir / f"{cache_key}.json"
        
        if cache_file.exists():
            with open(cache_file, "r") as f:
                return json.load(f)
                
        import tempfile
        import subprocess
        
        # Extract lightweight audio
        temp_audio = config.TEMP_DIR / f"whisper_audio_{cache_key}.wav"
        
        cmd = [config.get_ffmpeg_path(), "-y",
            "-ss", str(start),
            "-i", str(video_path),
            "-t", str(end - start),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(temp_audio)
        ]
        
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            logger.info("Transcribing segment %s-%s", start, end)
            model = cls.get_model()
            segments, info = model.transcribe(
                str(temp_audio), vad_filter=True, word_timestamps=True)
            segments = list(segments)
        finally:
            temp_audio.unlink(missing_ok=True)
        
        words_data = cls.group_words(segments, start, pacing_mode)
                
        with open(cache_file, "w") as f:
            json.dump(words_data, f, indent=2)
            
        return words_data

    @classmethod
    def transcribe(cls, video_path: Path) -> List[Dict]:
        """Transcribes the video and returns a list of word-level timestamps."""
        # Check cache first
        cache_dir = config.PROJECT_CACHE / "transcriptions"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Simple fingerprint: filename + size
        file_stat = video_path.stat()
        fingerprint = f"phrase-v2_{video_path.name}_{file_stat.st_size}"
        cache_key = hashlib.md5(fingerprint.encode()).hexdigest()
        cache_file = cache_dir / f"{cache_key}.json"
        
        if cache_file.exists():
            logger.info("Found cached transcription.")
            with open(cache_file, "r") as f:
                return json.load(f)
                
        logger.info(f"Transcribing {video_path.name} with faster-whisper...")
        model = cls.get_model()
        
        segments, info = model.transcribe(
            str(video_path), 
            vad_filter=True, 
            word_timestamps=True
        )
        
        logger.info(f"Detected language: {info.language} with probability {info.language_probability}")
        
        words_data = cls.group_words(segments)
                
        # Save to cache
        with open(cache_file, "w") as f:
            json.dump(words_data, f, indent=2)
            
        logger.info(f"Transcription complete. Found {len(words_data)} words.")
        return words_data
