import atexit
from app.config import config
import subprocess
import json
import logging
from pathlib import Path
from app.services.crop_service import CropSmoothingService
from typing import List, Dict
from typing import Optional
from app.models.schemas import VideoMetadata

logger = logging.getLogger(__name__)

class FFmpegService:
    active_processes = set()

    @classmethod
    def run_command(cls, cmd: list) -> subprocess.CompletedProcess:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cls.active_processes.add(process)
        try:
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        finally:
            cls.active_processes.discard(process)

    @classmethod
    def terminate_all(cls):
        for p in list(cls.active_processes):
            try:
                p.terminate()
            except Exception:
                pass
        
    @staticmethod
    def check_installed() -> bool:
        try:
            FFmpegService.run_command([config.get_ffmpeg_path(), "-version"])
            FFmpegService.run_command([config.get_ffprobe_path(), "-version"])
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def get_metadata(filepath: Path) -> VideoMetadata:
        cmd = [config.get_ffprobe_path(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(filepath)
        ]
        
        result = FFmpegService.run_command(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr}")
            
        data = json.loads(result.stdout)
        
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
        
        if not video_stream:
            raise ValueError("No video stream found")
            
        # Extract metadata
        format_info = data.get("format", {})
        duration = float(format_info.get("duration", 0))
        
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        fps_str = video_stream.get("r_frame_rate", "0/1")
        try:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if den != "0" else 0
        except ValueError:
            fps = 0
            
        video_codec = video_stream.get("codec_name", "")
        audio_codec = audio_stream.get("codec_name", "") if audio_stream else ""
        sample_rate = audio_stream.get("sample_rate") if audio_stream else None
        
        # rotation handling
        rotation = 0
        tags = video_stream.get("tags", {})
        side_data = video_stream.get("side_data_list", [])
        if "rotate" in tags:
            rotation = int(tags["rotate"])
        else:
            for sd in side_data:
                if "rotation" in sd:
                    rotation = int(sd["rotation"])
                    break
        
        file_size = int(format_info.get("size", 0))
        
        return VideoMetadata(
            filename=filepath.name,
            duration=duration,
            width=width,
            height=height,
            fps=fps,
            video_codec=video_codec,
            audio_codec=audio_codec,
            sample_rate=sample_rate,
            file_size=file_size,
            rotation=rotation
        )

    @staticmethod
    def create_proxy(source: Path, dest: Path, max_output_bytes: int | None = None,
                     progress_callback=None, source_duration: float | None = None,
                     is_speech_driven: bool | None = None) -> bool:
        import sys
        from app.services.proxy_encoder import encode_proxy_command
        duration = source_duration or FFmpegService.get_metadata(source).duration
        encoders = ["h264_videotoolbox", "libx264"] if sys.platform == "darwin" else ["libx264"]
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Policy: Semantic Analysis does not need 1080p60.
        fps = "15"
        scale = "480"
        crf = "28"
        vt_bitrate = "1500k"
        audio_bitrate = "96k"

        if is_speech_driven or (is_speech_driven is None and duration > 1200):
            # Aggressively compact for long form (podcasts, interviews, storytime)
            fps = "5"
            scale = "360"
            crf = "34"
            vt_bitrate = "250k"
            audio_bitrate = "48k"
        elif duration > 600:
            # Medium compact for >10m videos
            fps = "10"
            scale = "480"
            crf = "30"
            vt_bitrate = "600k"
            audio_bitrate = "64k"

        for encoder in encoders:
            if progress_callback:
                progress_callback(0, "Preparing video for analysis" if encoder == encoders[0]
                                  else "Hardware encoding failed; retrying with software encoding")
            cmd = [config.get_ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-nostdin",
                   "-y", "-threads", "4", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                   "-filter_threads", "2", "-vf", f"scale=-2:{scale},format=yuv420p", "-r", fps, "-c:v", encoder]
            if encoder == "h264_videotoolbox":
                cmd += ["-b:v", vt_bitrate]
            else:
                cmd += ["-threads", "2", "-crf", crf, "-preset", "veryfast"]
            cmd += ["-c:a", "aac", "-b:a", audio_bitrate, "-progress", "pipe:1", "-nostats"]
            if max_output_bytes is not None:
                cmd += ["-fs", str(max_output_bytes)]
            cmd.append(str(dest))
            log_path = config.PROJECT_LOGS / f"proxy-{dest.stem}-{encoder}.log"
            code = encode_proxy_command(cmd, duration, progress_callback,
                                        FFmpegService.active_processes, log_path)
            if code == 0:
                try:
                    actual_duration = FFmpegService.get_metadata(dest).duration
                    if actual_duration > 0 and actual_duration + 2 >= duration:
                        if progress_callback:
                            progress_callback(1, "Video prepared for analysis")
                        return True
                    logger.error("Incomplete proxy: expected=%.2fs actual=%.2fs", duration, actual_duration)
                except (OSError, ValueError, RuntimeError):
                    logger.exception("Proxy validation failed")
                # A size cap must never be reported as a complete source.
                if max_output_bytes is not None:
                    from app.services.storage_lifecycle import InsufficientStorage
                    dest.unlink(missing_ok=True)
                    raise InsufficientStorage("INSUFFICIENT_STORAGE")
            logger.error("Proxy encoding failed: encoder=%s exit=%s diagnostics=%s", encoder, code, log_path.name)
            dest.unlink(missing_ok=True)
        return False

    @staticmethod
    def _create_ass_file(subtitles: List[Dict], dest_path: Path, width: int = 1080, height: int = 1920) -> Path:
        ass_path = dest_path.with_suffix('.ass')
        
        # TikTok style ASS template
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans,95,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,4,2,20,20,500,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        def format_time(seconds: float) -> str:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            cs = int(round((seconds % 1) * 100))
            return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(header)
            for sub in subtitles:
                start = format_time(sub["start_seconds"])
                end = format_time(sub["end_seconds"])
                # Clean text of newlines
                text = sub["text"].replace("\n", " ").upper() 
                f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")
                
        return ass_path

    @staticmethod
    def _create_subtitle_images(subtitles: List[Dict], temp_dir: Path, duration: float,
                                width: int = 1080, prefix: str = "") -> dict:
        """Generate transparent PNGs for each subtitle using Pillow to bypass missing FFmpeg fonts."""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            logger.error("Pillow not installed")
            return []
            
        generated = []
        
        # Try to use a nice font, fallback to default
        font_size = int(width * 0.08) # 8% of video width
        font_path = Path(__file__).parent.parent / "assets" / "fonts" / "Roboto-Bold.ttf"
        try:
            font = ImageFont.truetype(str(font_path), font_size)
        except OSError:
            logger.error(f"Failed to load font at {font_path}")
            try:
                font = ImageFont.truetype("Arial Bold.ttf", font_size)
            except OSError:
                font = ImageFont.load_default()
                
        tag = prefix or str(id(subtitles))
        blank_path = temp_dir / f"blank_{tag}.png"
        img = Image.new('RGBA', (width, int(font_size * 3)), (0, 0, 0, 0))
        img.save(blank_path)
        generated.append({"path": blank_path})
        
        concat_path = temp_dir / f"subs_{tag}.ffconcat"
        
        with open(concat_path, "w") as f:
            f.write("ffconcat version 1.0\n")
            
            current_time = 0.0
            
            for i, sub in enumerate(subtitles):
                start = sub["start_seconds"]
                end = sub["end_seconds"]
                
                # Gap before subtitle
                if start > current_time:
                    f.write(f"file '{blank_path.name}'\n")
                    f.write(f"duration {start - current_time:.3f}\n")
                
                text = sub["text"].replace("\n", " ").upper()
                img_w, img_h = width, int(font_size * 3)
                img = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                words = text.split()
                if not words:
                    continue
                max_text_width = width * 0.82
                caption_font = font
                lines = [words]
                for size in range(font_size, max(28, int(width * 0.04)) - 1, -4):
                    try:
                        trial_font = ImageFont.truetype(str(font_path), size)
                    except OSError:
                        trial_font = font
                    options = [[words]]
                    options.extend([words[:split], words[split:]]
                                   for split in range(1, len(words)))
                    best = min(options, key=lambda option: max(
                        draw.textlength(" ".join(line), font=trial_font)
                        for line in option))
                    if max(draw.textlength(" ".join(line), font=trial_font)
                           for line in best) <= max_text_width:
                        caption_font, lines = trial_font, best
                        break
                else:
                    # Very long unbroken words are scaled to fit a two-line unit.
                    longest = max(draw.textlength(" ".join(line), font=caption_font)
                                  for line in lines)
                    if longest > max_text_width:
                        logger.warning("Caption too long for safe layout; skipping one unit")
                        continue

                line_height = int(caption_font.size * 1.16)
                top = (img_h - line_height * len(lines)) / 2
                import re
                normalize = lambda word: re.sub(r"[^\w]", "", word).upper()
                emphasis = {normalize(word) for word in sub.get("emphasis", [])}
                if not any(normalize(word) in emphasis for word in words):
                    # Discovery often has no emphasis metadata. Give every phrase
                    # one visual anchor, preferring numbers over longer content words.
                    stop_words = {"THE", "AND", "THAT", "THIS", "WITH", "FROM", "HAVE",
                                  "WAS", "WERE", "ARE", "YOU", "YOUR", "FOR", "BUT",
                                  "JUST", "THEN", "THEY", "IT", "IS", "TO", "OF", "A", "I",
                                  "ЭТО", "КАК", "ЧТО", "ЕГО", "ОНА", "ОНИ", "ДЛЯ", "ИЛИ"}
                    choices = [word for word in words if normalize(word) not in stop_words
                               and len(normalize(word)) > 2]
                    if choices:
                        anchor = max(choices, key=lambda word: (any(c.isdigit() for c in word),
                                                               len(normalize(word))))
                        emphasis = {normalize(anchor)}
                stroke_width = max(2, int(caption_font.size * 0.075))
                shadow_offset = max(2, int(caption_font.size * 0.045))
                for line_index, line in enumerate(lines):
                    line_width = draw.textlength(" ".join(line), font=caption_font)
                    x = (img_w - line_width) / 2
                    y = top + line_index * line_height
                    for word in line:
                        highlighted = normalize(word) in emphasis
                        color = "#FFD866" if highlighted else "#FFFFFF"
                        # A dark offset shadow keeps both colors legible over bright footage.
                        draw.text((x + shadow_offset, y + shadow_offset), word,
                                  font=caption_font, fill=(0, 0, 0, 180),
                                  stroke_width=stroke_width + 1, stroke_fill=(0, 0, 0, 180))
                        draw.text((x, y), word, font=caption_font, fill=color,
                                  stroke_width=stroke_width, stroke_fill="black")
                        x += draw.textlength(word + " ", font=caption_font)
                
                png_path = temp_dir / f"sub_overlay_{tag}_{i}.png"
                img.save(png_path)
                generated.append({"path": png_path})
                if sub.get("motion") == "SOFT_SCALE" and end - start > .35:
                    from PIL import Image as PillowImage
                    intro = img.resize((int(img_w * .96), int(img_h * .96)),
                                       PillowImage.Resampling.LANCZOS)
                    intro_canvas = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
                    intro_canvas.alpha_composite(
                        intro, ((img_w - intro.width) // 2,
                                (img_h - intro.height) // 2))
                    intro_path = temp_dir / f"sub_intro_{tag}_{i}.png"
                    intro_canvas.save(intro_path)
                    generated.append({"path": intro_path})
                    f.write(f"file '{intro_path.name}'\n")
                    f.write("duration 0.120\n")
                f.write(f"file '{png_path.name}'\n")
                f.write(f"duration {end - start - (0.12 if sub.get('motion') == 'SOFT_SCALE' and end - start > .35 else 0):.3f}\n")
                
                current_time = end
            
            # Pad the end of the ffconcat stream to exactly match the video duration
            # This prevents FFmpeg overlay from looping the last frame forever, or ending the video early!
            if current_time < duration:
                f.write(f"file '{blank_path.name}'\n")
                f.write(f"duration {duration - current_time:.3f}\n")
                
            # The concat demuxer requires the last file to be repeated without duration
            f.write(f"file '{blank_path.name}'\n")
            
        return {"concat_path": concat_path, "images": generated}

    @staticmethod
    def render_clip(source: Path, dest: Path, candidate_data: dict, aspect_mode: str, output_resolution: str, subtitles: bool = False, progress_callback=None) -> bool:
        segments = candidate_data.get("source_segments", [])
        if not segments:
            return False
            
        temp_files = []
        ephemeral_files = []
        temp_dir = dest.parent
        
        shot_plan = candidate_data.get("shot_plan")
        if not shot_plan:
            # Fallback to source segments if no shot plan
            shot_plan = [{"start": s["start_seconds"], "end": s["end_seconds"], "target_track_ids": []} for s in segments]
        source_meta = FFmpegService.get_metadata(source)
        render_fps = 60 if source_meta.fps >= 50 else 30
            
        try:
            for idx, shot in enumerate(shot_plan):
                start = shot["start"]
                end = shot["end"]
                duration = end - start
                
                temp_file = temp_dir / f"temp_{dest.stem}_part{idx}.mp4"
                temp_files.append(temp_file)
                previous_contiguous = (idx > 0 and
                                       abs(shot_plan[idx-1]["end"] - start) < .04)
                next_contiguous = (idx + 1 < len(shot_plan) and
                                   abs(shot_plan[idx+1]["start"] - end) < .04)
                
                cmd = [config.get_ffmpeg_path(), "-y",
                    "-ss", str(start),
                    "-i", str(source),
                    "-t", str(duration),
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-video_track_timescale", "90000",
                    "-movflags", "+faststart"
                ]
                # A virtual camera cut must not dip an otherwise continuous
                # spoken word. Fades belong only at true source-time EDL cuts.
                fades = []
                fade_len = min(.05, duration / 4)
                if not previous_contiguous:
                    fades.append(f"afade=t=in:st=0:d={fade_len:.3f}")
                if not next_contiguous:
                    fades.append(f"afade=t=out:st={duration-fade_len:.3f}:d={fade_len:.3f}")
                if fades:
                    cmd.extend(["-af", ",".join(fades)])

                if output_resolution == "1080x1920" or task.get("is_stories_mode"):
                    target_w, target_h = 1080, 1920
                elif output_resolution == "720x1280":
                    target_w, target_h = 720, 1280
                else:
                    target_w, target_h = 1080, 1920

                # Find average face position for the TARGET tracks in this segment
                face_tracks = candidate_data.get("face_tracks", [])
                target_ids = shot.get("target_track_ids", [])
                
                crop_x = "(in_w-out_w)/2"
                crop_y = "(in_h-out_h)/2"
                
                sendcmd_file = None
                x_centers = []
                y_centers = []
                
                for f in face_tracks:
                    if start <= f["time"] <= end:
                        for face in f.get("all_faces", []):
                            if not target_ids or face.get("track_id") in target_ids:
                                x_centers.append(face["x"] + face["w"]/2)
                                y_centers.append(face["y"] + face["h"]/3) # Upper third
                                
                if x_centers and y_centers:
                    avg_y = sum(y_centers) / len(y_centers)
                    crop_y = f"max(0\\, min(in_h-out_h\\, in_h*{avg_y} - out_h/2))"
                    
                # Dynamic Tracking Check
                # Calculate the width after the existing height normalization.
                # crop_h = 1080, crop_w = 1080 * 9/16 = 608
                source_w, source_h = source_meta.width, source_meta.height
                if abs(source_meta.rotation) % 180 == 90:
                    source_w, source_h = source_h, source_w
                internal_source_w = max(2, int(round(
                    source_w * target_h / max(1, source_h) / 2) * 2))
                internal_crop_w = target_w
                
                normalize = f"scale=-2:{target_h}"
                if internal_source_w < internal_crop_w:
                    normalize = f"scale={internal_crop_w}:-2"
                    internal_source_w = internal_crop_w

                if x_centers and internal_source_w > internal_crop_w:
                    sendcmd_path = temp_dir / f"sendcmd_{dest.stem}_part{idx}.txt"
                    ephemeral_files.append(sendcmd_path)
                    
                    CropSmoothingService.generate_shot_sendcmd(
                        shot=shot,
                        face_tracks=face_tracks,
                        video_width=internal_source_w,
                        crop_width=internal_crop_w,
                        output_file=str(sendcmd_path),
                        fps=render_fps,
                    )
                    if sendcmd_path.stat().st_size:
                        sendcmd_file = sendcmd_path

                shot_composition = shot.get("composition", "medium").lower()
                current_aspect_mode = aspect_mode
                if current_aspect_mode not in ("center_crop", "fit_blur", "original"):
                    current_aspect_mode = "center_crop"

                subtitles = candidate_data.get("subtitles", [])
                segment_subs = []
                for sub in subtitles:
                    if sub["start_seconds"] < end and sub["end_seconds"] > start:
                        rel_start = max(0.0, sub["start_seconds"] - start)
                        rel_end = min(duration, sub["end_seconds"] - start)
                        if rel_end > rel_start:
                            segment_subs.append({
                                "start_seconds": rel_start,
                                "end_seconds": rel_end,
                                "text": sub["text"],
                                "emphasis": sub.get("emphasis", []),
                                "motion": sub.get("motion", "APPEAR"),
                            })
                
                sub_data = FFmpegService._create_subtitle_images(
                    segment_subs, temp_dir, duration, target_w,
                    prefix=f"{dest.stem}_part{idx}") if segment_subs else None
                if sub_data:
                    ephemeral_files.append(Path(sub_data["concat_path"]))
                    ephemeral_files.extend(Path(img["path"]) for img in sub_data.get("images", []))
                has_subs = bool(sub_data and sub_data.get("images"))
                context = candidate_data.get("context_overlay") or {}
                context_subs = []
                if context and context["start_seconds"] < end and context["end_seconds"] > start:
                    context_subs = [{
                        "start_seconds": max(0.0, context["start_seconds"] - start),
                        "end_seconds": min(duration, context["end_seconds"] - start),
                        "text": context["text"],
                    }]
                context_data = FFmpegService._create_subtitle_images(
                    context_subs, temp_dir, duration, target_w,
                    prefix=f"{dest.stem}_context_part{idx}") if context_subs else None
                if context_data:
                    ephemeral_files.append(Path(context_data["concat_path"]))
                    ephemeral_files.extend(Path(img["path"])
                                           for img in context_data.get("images", []))
                
                filter_complex = ""
                if current_aspect_mode == "center_crop":
                    if sendcmd_file and sendcmd_file.exists():
                        # CROP FIRST -> THEN SCALE
                        filter_complex = f"[0:v]{normalize},sendcmd=f='{sendcmd_file}',crop@c={internal_crop_w}:{target_h}:x:(ih-oh)/2,scale={target_w}:{target_h},setsar=1"
                    else:
                        filter_complex = f"[0:v]{normalize},crop={internal_crop_w}:{target_h}:(iw-ow)/2:(ih-oh)/2,scale={target_w}:{target_h},setsar=1"
                elif current_aspect_mode == "fit_blur":
                    filter_complex = f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h}:{crop_x}:{crop_y},boxblur=20:20[bg];[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,setsar=1"
                else:
                    filter_complex = f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"

                # Every part must have the same frame rate before stream-copy concat.
                filter_complex += f",fps={render_fps}"

                zoom_intent = shot.get("zoom_intent", "NONE")
                effect = shot.get("effect")
                base_zoom = (1.08 if shot_composition == "close" else
                             1.04 if shot_composition == "medium_close" else 1.0)
                if current_aspect_mode == "center_crop" and effect:
                    start_frame = max(0, int((effect["source_start"] - start) * render_fps))
                    end_frame = min(max(1, int(duration * render_fps)),
                                    int((effect["source_end"] - start) * render_fps))
                    amplitude = min(1.12 - base_zoom,
                                    max(0, float(effect["intensity"])))
                    if effect["effect_type"] == "IMPACT":
                        amplitude = min(amplitude, .04)
                    if end_frame > start_frame and amplitude > 0:
                        progress = f"(on-{start_frame})/{end_frame-start_frame}"
                        if effect["effect_type"] == "PUNCH_OUT":
                            zoom = (f"if(lt(on\\,{end_frame})\\,"
                                    f"{base_zoom:.4f}+{amplitude:.4f}*(1-{progress})*"
                                    f"(1-{progress})\\,{base_zoom:.4f})")
                        elif (effect["effect_type"] == "SLOW_PUSH" and
                              end_frame >= int(duration * render_fps) - 2):
                            zoom = (f"{base_zoom:.4f}+{amplitude:.4f}*min(1\\,max(0\\,"
                                    f"{progress}))")
                        else:
                            zoom = (f"if(between(on\\,{start_frame}\\,{end_frame})\\,"
                                    f"{base_zoom:.4f}+{amplitude:.4f}*sin(PI*{progress})\\,"
                                    f"{base_zoom:.4f})")
                        filter_complex += (f",zoompan=z='{zoom}':"
                                           f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
                                           f"d=1:s={target_w}x{target_h}:fps={render_fps}")
                elif current_aspect_mode == "center_crop" and zoom_intent in {"SUBTLE_IN", "SUBTLE_OUT"}:
                    frame_count = max(1, int(duration * render_fps))
                    if zoom_intent == "SUBTLE_IN":
                        zoom = f"min(1.08,1+on*0.08/{frame_count})"
                    else:
                        zoom = f"max(1,1.08-on*0.08/{frame_count})"
                    filter_complex += (f",zoompan=z='{zoom}':"
                                       f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
                                       f"d=1:s={target_w}x{target_h}:fps={render_fps}")
                elif current_aspect_mode == "center_crop" and base_zoom > 1:
                    filter_complex += (f",zoompan=z='{base_zoom:.4f}':"
                                       f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
                                       f"d=1:s={target_w}x{target_h}:fps={render_fps}")
                
                overlays = []
                if has_subs:
                    important_boxes = [face for track in face_tracks
                                       if start <= track.get("time", -1) <= end
                                       for face in track.get("all_faces", [])
                                       if not target_ids or face.get("track_id") in target_ids]
                    collides = (current_aspect_mode == "center_crop" and any(
                        face.get("y", 0) + face.get("h", 0) > .72 and
                        face.get("y", 0) < .9 for face in important_boxes))
                    overlays.append((sub_data, str(shot.get("caption_y", .59 if collides else .75))))
                if context_data and context_data.get("images"):
                    overlays.append((context_data, "0.12"))
                if overlays:
                    filter_complex += "[v0]"
                    for input_index, (data, vertical_position) in enumerate(overlays, 1):
                        c_v_idx = cmd.index("-c:v")
                        cmd[c_v_idx:c_v_idx] = ["-f", "concat", "-safe", "0",
                                                "-i", str(data["concat_path"])]
                        output_label = (f"[v{input_index}]" if
                                        input_index < len(overlays) else "")
                        filter_complex += (
                            f";[v{input_index - 1}][{input_index}:v]"
                            f"overlay=(main_w-overlay_w)/2:(main_h*{vertical_position})"
                            f"{output_label}")

                if not source_meta.audio_codec:
                    input_position = cmd.index("-c:v")
                    cmd[input_position:input_position] = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                filter_complex += "[rendered]"
                cmd.extend(["-filter_complex", filter_complex, "-map", "[rendered]",
                            "-map", "0:a:0" if source_meta.audio_codec else f"{1 + len(overlays)}:a:0"])

                logger.info(f"FINAL PRODUCTION FILTER GRAPH: {filter_complex}")
                # Ensure the output duration strictly matches the expected duration
                cmd.extend(["-t", str(duration)])
                cmd.append(str(temp_file))
                
                
                if progress_callback:
                    cmd.insert(1, "-progress")
                    cmd.insert(2, "pipe:1")
                    
                    import subprocess
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    
                    import re
                    time_pattern = re.compile(r"out_time_us=(\d+)")
                    duration_us = duration * 1000000
                    
                    for line in process.stdout:
                        if "out_time_us=" in line:
                            match = time_pattern.search(line)
                            if match:
                                us = int(match.group(1))
                                if us > 0 and duration_us > 0:
                                    p = min(1.0, us / duration_us)
                                    # Calculate overall progress across all shots (0.3 to 1.0)
                                    base_p = 0.3 + (0.7 * (idx / len(shot_plan)))
                                    shot_p = (0.7 / len(shot_plan)) * p
                                    progress_callback(base_p + shot_p, f"Rendering shot {idx+1} of {len(shot_plan)}")
                    
                    process.wait()
                    class DummyRes:
                        def __init__(self, code, err):
                            self.returncode = code
                            self.stderr = err
                    res = DummyRes(process.returncode, 'Check stdout logs for error')
                else:
                    res = FFmpegService.run_command(cmd)
                
                for data, _ in overlays:
                    for img in data["images"]:
                        Path(img["path"]).unlink(missing_ok=True)
                    Path(data["concat_path"]).unlink(missing_ok=True)
                        
                if res.returncode != 0:
                    logger.error(f"Failed to render segment {idx}: {res.stderr}")
                    return False
                    
            # Concatenate
            if len(temp_files) == 1:
                temp_files[0].rename(dest)
            else:
                concat_list = temp_dir / f"concat_{dest.stem}.txt"
                with open(concat_list, "w") as f:
                    for tf in temp_files:
                        f.write(f"file '{tf.absolute()}'\n")
                        
                concat_cmd = [config.get_ffmpeg_path(), "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(dest)
                ]
                res = FFmpegService.run_command(concat_cmd)
                concat_list.unlink(missing_ok=True)
                # Cleanup temp files
                for tf in temp_files:
                    if tf.exists():
                        tf.unlink(missing_ok=True)
                if res.returncode != 0:
                    logger.error(f"Concat failed: {res.stderr}")
                    return False
            # Generate final concatenated subtitle file if needed
            subtitles = candidate_data.get("subtitles", [])
            if subtitles:
                from app.services.timeline import remap_intervals
                final_subs = remap_intervals(shot_plan, subtitles)
                    
                if final_subs:
                    ass_file = dest.with_suffix('.ass')
                    ephemeral_files.append(ass_file)
                    FFmpegService._create_ass_file(final_subs, ass_file, target_w, target_h)

            return True
        finally:
            for tf in temp_files:
                if tf.exists() and tf != dest:
                    tf.unlink(missing_ok=True)
            for path in ephemeral_files:
                path.unlink(missing_ok=True)

    @staticmethod
    def generate_thumbnail(source: Path, dest: Path, time_offset: float = 0.5) -> bool:
        cmd = [config.get_ffmpeg_path(), "-y",
            "-ss", str(time_offset),
            "-i", str(source),
            "-vframes", "1",
            "-vf", "scale=320:-1",
            str(dest)
        ]
        result = FFmpegService.run_command(cmd)
        return result.returncode == 0

    @staticmethod
    def generate_smoke_test_video(dest: Path) -> bool:
        cmd = [config.get_ffmpeg_path(), "-y",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=1280x720:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=5",
            "-c:v", "libx264",
            "-c:a", "aac",
            str(dest)
        ]
        result = FFmpegService.run_command(cmd)
        return result.returncode == 0

atexit.register(FFmpegService.terminate_all)
