import os
import hashlib
import json
from pathlib import Path
from typing import List, Dict
import logging

from scenedetect import detect, ContentDetector

from app.config import config

logger = logging.getLogger(__name__)

class SceneDetectionService:
    @classmethod
    def detect_scenes(cls, video_path: Path, threshold: float = 30.0) -> List[Dict]:
        cache_dir = config.PROJECT_CACHE / "scenes"
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        file_stat = video_path.stat()
        fingerprint = f"{video_path.name}_{file_stat.st_size}_{threshold}"
        cache_key = hashlib.md5(fingerprint.encode()).hexdigest()
        cache_file = cache_dir / f"{cache_key}.json"
        
        if cache_file.exists():
            with open(cache_file, "r") as f:
                return json.load(f)
                
        logger.info(f"Detecting scenes in {video_path.name}...")
        scene_list = detect(str(video_path), ContentDetector(threshold=threshold))
        
        scenes_data = []
        for i, scene in enumerate(scene_list):
            scenes_data.append({
                "scene_num": i + 1,
                "start_seconds": scene[0].get_seconds(),
                "end_seconds": scene[1].get_seconds()
            })
            
        with open(cache_file, "w") as f:
            json.dump(scenes_data, f, indent=2)
            
        return scenes_data
