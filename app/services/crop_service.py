import bisect
import collections
import math
import statistics

class SmoothedCameraman:
    def __init__(self, crop_width, video_width, fps=30):
        self.crop_width = crop_width
        self.video_width = video_width
        self.fps = fps
        
        # Filtering parameters
        self.median_window_size = 5
        self.ema_alpha = 1 - 0.7 ** (30 / fps)
        
        # History
        self.raw_history = collections.deque(maxlen=self.median_window_size)
        self.ema_target = None
        self.current_center_x = video_width / 2
        
        # Dead zone & Hysteresis
        self.move_start_threshold = crop_width * 0.15
        self.move_stop_threshold = crop_width * 0.05
        
        # Camera Lock
        self.lock_tolerance = crop_width * 0.05
        self.unlock_threshold = crop_width * 0.20
        self.stationary_frames_for_lock = int(fps * 1.5)  # 1.5 seconds
        
        # Speeds
        self.base_pan_speed = crop_width * 0.015 * 30 / fps  # constant speed per second
        self.emergency_threshold = crop_width * 0.35
        self.emergency_multiplier = 3.0
        
        # State
        self.is_moving = False
        self.is_locked = False
        self.stationary_frame_count = 0
        self.was_tracking = False
        self._snap_pending = True
        
        # Direction reversal protection
        self.last_move_direction = 0  # 1 for right, -1 for left
        self.direction_persistence = 0
        self.required_persistence = max(1, round(fps * .1))
        
        # Metrics
        self.metrics = {
            "target_loss_count": 0,
            "target_reacquisition_count": 0,
            "camera_move_count": 0,
            "micro_reversal_count": 0,
            "camera_lock_duration": 0,
            "emergency_catchup_count": 0,
            "raw_target_variance": 0.0,
            "filtered_target_variance": 0.0
        }
        self.all_raw_targets = []
        self.all_filtered_targets = []
        
    def begin_scene(self):
        self.raw_history.clear()
        self.ema_target = None
        self.is_moving = False
        self.is_locked = False
        self.stationary_frame_count = 0
        self.was_tracking = False
        self._snap_pending = True
        self.last_move_direction = 0
        self.direction_persistence = 0
        self.metrics = {
            "target_loss_count": 0,
            "target_reacquisition_count": 0,
            "camera_move_count": 0,
            "micro_reversal_count": 0,
            "camera_lock_duration": 0,
            "emergency_catchup_count": 0,
            "raw_target_variance": 0.0,
            "filtered_target_variance": 0.0
        }
        self.all_raw_targets = []
        self.all_filtered_targets = []
        
    def update_target(self, new_center_x):
        if new_center_x is None:
            if self.was_tracking:
                self.metrics["target_loss_count"] += 1
                self.was_tracking = False
            return
            
        if not self.was_tracking:
            self.metrics["target_reacquisition_count"] += 1
            self.was_tracking = True
            
        self.all_raw_targets.append(new_center_x)
        self.raw_history.append(new_center_x)
        
        # 1. Median Filter
        median_target = statistics.median(self.raw_history)
        
        # 2. Snap or EMA
        if self._snap_pending:
            self._snap_pending = False
            self.ema_target = median_target
            self.current_center_x = median_target
        else:
            self.ema_target = self.ema_target * (1 - self.ema_alpha) + median_target * self.ema_alpha
            
        self.all_filtered_targets.append(self.ema_target)
        
    def get_crop_x(self):
        if self.ema_target is None:
            return self._clamp_crop_x(self.current_center_x)
            
        displacement = self.ema_target - self.current_center_x
        abs_disp = abs(displacement)
        
        # 3. Static Shot Lock Logic
        if abs_disp < self.lock_tolerance and not self.is_moving:
            self.stationary_frame_count += 1
            if self.stationary_frame_count >= self.stationary_frames_for_lock:
                self.is_locked = True
        else:
            if self.is_locked and abs_disp > self.unlock_threshold:
                self.is_locked = False
                self.stationary_frame_count = 0
            elif not self.is_locked:
                self.stationary_frame_count = 0
                
        if self.is_locked:
            self.metrics["camera_lock_duration"] += 1
            return self._clamp_crop_x(self.current_center_x)
            
        # 4. Hysteresis (Start/Stop Moving)
        if not self.is_moving and abs_disp > self.move_start_threshold:
            self.is_moving = True
        elif self.is_moving and abs_disp < self.move_stop_threshold:
            self.is_moving = False
            
        if not self.is_moving:
            return self._clamp_crop_x(self.current_center_x)
            
        # 5. Direction Reversal Protection
        direction = 1 if displacement > 0 else -1
        if direction != self.last_move_direction:
            self.direction_persistence += 1
            if self.direction_persistence < self.required_persistence:
                self.metrics["micro_reversal_count"] += 1
                return self._clamp_crop_x(self.current_center_x)
            self.last_move_direction = direction
            self.direction_persistence = 0
        else:
            self.direction_persistence = 0
            
        # 6. Velocity & Emergency Catchup
        pan_speed = self.base_pan_speed
        if abs_disp > self.emergency_threshold:
            pan_speed *= self.emergency_multiplier
            self.metrics["emergency_catchup_count"] += 1
            
        # Move camera
        if abs_disp > pan_speed:
            self.current_center_x += direction * pan_speed
            self.metrics["camera_move_count"] += 1
        elif abs_disp > 0:
            self.current_center_x = self.ema_target
            self.metrics["camera_move_count"] += 1
            
        return self._clamp_crop_x(self.current_center_x)

    def _clamp_crop_x(self, center_x):
        half_crop = self.crop_width / 2
        clamped_center = center_x
        if clamped_center - half_crop < 0:
            clamped_center = half_crop
        if clamped_center + half_crop > self.video_width:
            clamped_center = self.video_width - half_crop
        return max(0, int(clamped_center - half_crop))
class CropSmoothingService:
    @staticmethod
    def generate_shot_sendcmd(shot, face_tracks, video_width, crop_width, output_file, fps=30):
        cameraman = SmoothedCameraman(crop_width, video_width, fps=fps)
        cameraman.begin_scene()
        
        start = shot["start"]
        end = shot["end"]
        duration = end - start
        # A valid shot can be shorter than one output frame. FFmpeg's sendcmd
        # filter rejects an empty script, so always emit the initial crop.
        frames = max(1, math.ceil(duration * fps))
        
        target_ids = shot.get("target_track_ids", [])
        
        # Tracking is sampled at 2 fps. Interpolate adjacent observations so
        # the camera gets a continuous target instead of intermittent detections.
        samples = []
        for frame in sorted(face_tracks, key=lambda f: f["time"]):
            if not start <= frame["time"] < end:
                continue
            visible = [face for face in frame.get("all_faces", [])
                       if not target_ids or face.get("track_id") in target_ids]
            if visible:
                left = min(face["x"] for face in visible)
                right = max(face["x"] + face["w"] for face in visible)
                samples.append((frame["time"], (left + right) * video_width / 2))
        times = [sample[0] for sample in samples]
        # Initialize framing before the first output frame; never snap halfway
        # through a shot when the first tracker observation arrives late.
        cameraman.update_target(samples[0][1] if samples and times[0] - start <= .75
                                else video_width / 2)
        lines = []
        last_crop_x = None

        for frame_idx in range(frames):
            time_sec = start + frame_idx / fps
            index = bisect.bisect_right(times, time_sec) - 1
            target = None
            if index >= 0 and time_sec - times[index] <= .75:
                target = samples[index][1]
                if index + 1 < len(samples) and 0 < times[index + 1] - times[index] <= .75:
                    fraction = (time_sec - times[index]) / (times[index + 1] - times[index])
                    target += (samples[index + 1][1] - target) * fraction
            cameraman.update_target(target)

            crop_x = cameraman.get_crop_x()
            
            # Deduplicate sendcmd lines (only write when x changes meaningfully)
            min_camera_delta = max(2, int(crop_width * 0.005))
            if last_crop_x is None or abs(crop_x - last_crop_x) >= min_camera_delta:
                rel_time = frame_idx / fps
                lines.append(f"{rel_time:.3f} crop@c x {crop_x};")
                last_crop_x = crop_x
                
        with open(output_file, "w") as f:
            f.write("\n".join(lines))
            
        return output_file
