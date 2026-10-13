from app.config import config
import subprocess
import json
import logging
import cv2
from pathlib import Path
from typing import List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

class LocalVideoAnalyzer:
    @staticmethod
    def detect_silences(filepath: Path, noise_level: str = "-30dB", duration: float = 1.0, segments: list = None) -> List[Dict[str, float]]:
        """Detect silence intervals in the audio."""
        if segments is None:
            # Fake a single segment covering a huge range if none provided
            segments = [(0.0, 999999.0)]
            
        silences = []
        for start_sec, end_sec in segments:
            seg_duration = end_sec - start_sec
            cmd = [config.get_ffmpeg_path(),
                "-ss", str(start_sec),
                "-t", str(seg_duration),
                "-i", str(filepath),
                "-af", f"silencedetect=noise={noise_level}:d={duration}",
                "-f", "null", "-"
            ]
            
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = result.stderr.splitlines()
            
            current_start = None
            
            for line in lines:
                if "silencedetect" in line:
                    if "silence_start" in line:
                        parts = line.split("silence_start: ")
                        if len(parts) > 1:
                            # ffmpeg outputs relative time, add start_sec
                            current_start = float(parts[1].split()[0]) + start_sec
                    elif "silence_end" in line and current_start is not None:
                        parts = line.split("silence_end: ")
                        if len(parts) > 1:
                            end_str = parts[1].split()[0]
                            try:
                                end = float(end_str) + start_sec
                                silences.append({"start": current_start, "end": end})
                            except ValueError:
                                pass
                            current_start = None
        
        return silences

    @staticmethod
    def detect_speech(filepath: Path, duration: float, segments: list = None) -> List[Dict[str, float]]:
        """Invert silence intervals to return speech intervals."""
        silences = LocalVideoAnalyzer.detect_silences(filepath, segments=segments)
        return LocalVideoAnalyzer.speech_from_silences(silences, duration, segments)

    @staticmethod
    def speech_from_silences(silences: list, duration: float,
                             segments: list = None) -> List[Dict[str, float]]:
        speech = []
        
        if segments is None:
            segments = [(0.0, duration)]
            
        for start_sec, end_sec in segments:
            current_time = start_sec
            # Filter silences that overlap with this segment
            seg_silences = sorted(
                ({"start": max(start_sec, s["start"]), "end": min(end_sec, s["end"])}
                 for s in silences if s["start"] < end_sec and s["end"] > start_sec),
                key=lambda s: s["start"])
            
            for s in seg_silences:
                if s["start"] > current_time:
                    speech.append({"start": current_time, "end": s["start"]})
                current_time = max(current_time, s["end"])
                
            if current_time < end_sec:
                speech.append({"start": current_time, "end": end_sec})
            
        return speech

    @staticmethod
    def detect_scenes(filepath: Path, threshold: float = 0.3, segments: list = None) -> List[float]:
        """Detect scene change timestamps."""
        if segments is None:
            segments = [(0.0, 999999.0)]
            
        scene_cuts = []
        for start_sec, end_sec in segments:
            seg_duration = end_sec - start_sec
            cmd = [config.get_ffmpeg_path(),
                "-ss", str(start_sec),
                "-t", str(seg_duration),
                "-i", str(filepath),
                "-filter:v", f"select='gt(scene,{threshold})',showinfo",
                "-f", "null", "-"
            ]
            
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = result.stderr.splitlines()
            
            for line in lines:
                if "Parsed_showinfo" in line and "pts_time:" in line:
                    try:
                        time_str = line.split("pts_time:")[1].split()[0]
                        scene_cuts.append(float(time_str) + start_sec)
                    except (IndexError, ValueError):
                        pass
                    
        return scene_cuts

    @staticmethod
    def track_faces(filepath: Path, segments: List[tuple], sample_rate_fps: int = 2) -> List[Dict]:
        """
        Tracks people/faces in the given segments using Ultralytics YOLO object tracking.
        Returns a list of dicts with timestamp, bounding boxes, and track_ids.
        """
        if not segments:
            return []
            
        import cv2
        import subprocess
        import tempfile
        import os
        from ultralytics import YOLO
        from app.config import config
        import logging
        logger = logging.getLogger(__name__)

        cap = cv2.VideoCapture(str(filepath))
        seg_cap, temp_path = None, None
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = 30
            
            frame_interval = int(fps / sample_rate_fps)
            if frame_interval < 1:
                frame_interval = 1
            
            # Initialize YOLO model. This enforces project-local model downloads.
            yolo_dir = config.PROJECT_MODELS / "yolo"
            yolo_dir.mkdir(parents=True, exist_ok=True)
            model_path = yolo_dir / "yolov8n.pt"
        
            # Download model if not exists to avoid absolute path crash
            if not model_path.exists():
                import urllib.request
                import fcntl
                lock_path = yolo_dir / "yolov8n.pt.lock"
                with open(lock_path, "w") as lock_file:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                    if not model_path.exists(): # Check again after acquiring lock
                        logger.info(f"Downloading YOLOv8n to {model_path}...")
                        urllib.request.urlretrieve("https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt", str(model_path))
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
            
            logger.info(f"Loading YOLO model from {model_path}")
            model = YOLO(str(model_path))
        
            tracks = []
        
            for start_sec, end_sec in segments:
                duration = end_sec - start_sec
            
                # Extract segment using fast FFmpeg seek
                temp_fd, temp_path = tempfile.mkstemp(suffix=".mp4", dir=str(config.TEMP_DIR))
                os.close(temp_fd)
            
                cmd = [
                    config.get_ffmpeg_path(), "-y",
                    "-ss", str(start_sec),
                    "-t", str(duration),
                    "-i", str(filepath),
                    "-vf", "scale=-2:480", # Downscale to 480p for lightning-fast tracking
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "28",
                    "-an",
                    temp_path
                ]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode != 0:
                    logger.error(f'FFmpeg failed: {res.stderr}')
            
                seg_cap = cv2.VideoCapture(temp_path)
            
                current_frame = 0
                sampled_frames = 0
                while True:
                    ret, frame = seg_cap.read()
                    if not ret:
                        break
                    
                    if current_frame % frame_interval != 0:
                        current_frame += 1
                        continue
                
                    # YOLO tracking natively handles frame-to-frame IDs
                    results = model.track(frame, persist=sampled_frames > 0, verbose=False)
                    sampled_frames += 1
                
                    time_sec = start_sec + (current_frame / fps)
                
                    if len(results) > 0 and results[0].boxes and results[0].boxes.id is not None:
                        h, w = frame.shape[:2]
                        boxes_data = results[0].boxes
                    
                        tracked_faces = []
                        for i in range(len(boxes_data)):
                            # YOLO format: xywh normalized? No, it returns pixels by default
                            box_tensor = boxes_data.xyxy[i]
                            track_id = int(boxes_data.id[i].item())
                            class_id = int(boxes_data.cls[i].item())
                            class_name = model.names[class_id]
                        
                            bx1, by1, bx2, by2 = box_tensor.tolist()
                            bw = bx2 - bx1
                            bh = by2 - by1
                        
                            # We want a "face" crop but we detected a "person".
                            # We assume the face is in the upper third of the person box.
                            # Let's adjust the box to focus on the upper body/face area for better framing.
                            face_y = by1
                            face_h = bh * 0.4 if class_name == "person" else bh
                        
                            t_box = {
                                "track_id": track_id,
                                "class_id": class_id,
                                "class_name": class_name,
                                "x": bx1 / w,
                                "y": face_y / h,
                                "w": bw / w,
                                "h": face_h / h
                            }
                            tracked_faces.append(t_box)
                    
                        if tracked_faces:
                            tracked_faces.sort(key=lambda b: b["w"] * b["h"], reverse=True)
                            tracks.append({
                                "time": time_sec,
                                "primary_face": tracked_faces[0],
                                "all_faces": tracked_faces
                            })
                    else:
                        # If YOLO lost tracking, rely on VisualDirector's freeze mechanism or previous logic
                        pass
                        
                    current_frame += 1
                
                seg_cap.release()
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                
            cap.release()
            return tracks
        finally:
            cap.release()
            if seg_cap is not None:
                seg_cap.release()
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)
