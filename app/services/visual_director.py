import logging
from typing import List, Dict, Any, Optional
from app.models.schemas import ClipCandidate, SemanticVisualTarget

logger = logging.getLogger(__name__)

class VirtualShot:
    def __init__(self, start: float, end: float, target_track_ids: List[int],
                 composition: str = "medium", subject: str = "full_scene",
                 zoom_intent: str = "NONE"):
        self.start = start
        self.end = end
        self.target_track_ids = target_track_ids
        self.composition = composition
        self.subject = subject
        self.zoom_intent = zoom_intent
        
    def to_dict(self):
        return {
            "start": self.start,
            "end": self.end,
            "target_track_ids": self.target_track_ids,
            "composition": self.composition,
            "subject": self.subject,
            "zoom_intent": self.zoom_intent,
        }

class VisualDirector:
    MIN_SHOT_SECONDS = {
        "HYPER": .8, "FAST": 1.0, "ENERGETIC": 1.2,
        "CONVERSATIONAL": 1.8, "STORY": 2.0, "TENSION": 2.3,
        "EMOTIONAL": 2.4, "COMEDIC": 1.7, "EDUCATIONAL": 1.5,
        "VISUAL": 1.4,
    }

    def __init__(self, min_shot_duration: float = 1.5):
        self.min_shot_duration = min_shot_duration

    @classmethod
    def for_pacing_mode(cls, mode: str):
        return cls(cls.MIN_SHOT_SECONDS.get(mode.upper(), 1.5))

    @staticmethod
    def _tracks_for_segment(tracks: List[Dict[str, Any]]):
        positions = {}
        classes = {}
        for track in tracks:
            for target in track.get("all_faces", []):
                track_id = target.get("track_id")
                if track_id is None:
                    continue
                positions.setdefault(track_id, []).append(target["x"])
                classes[track_id] = target.get("class_name", "person")
        average = {track_id: sum(xs) / len(xs)
                   for track_id, xs in positions.items()}
        people = sorted(((track_id, x) for track_id, x in average.items()
                         if classes[track_id] == "person"), key=lambda item: item[1])
        speakers = {f"SPEAKER_{chr(65 + index)}": track_id
                    for index, (track_id, _) in enumerate(people[:3])}
        objects = {}
        for track_id in average:
            if classes[track_id] != "person":
                objects.setdefault(classes[track_id], []).append(track_id)
        return classes, speakers, objects, list(average)

    def _scene_boundaries(self, start: float, end: float,
                          scene_cuts: List[float]) -> List[float]:
        # The scene detector can mark almost every frame during fast motion.
        # Treat nearby marks as one cut so the renderer does not create
        # one-frame shots or turn natural motion into a flickering montage.
        boundaries = [start]
        for cut in sorted(set(scene_cuts)):
            if cut - boundaries[-1] >= self.min_shot_duration and \
                    end - cut >= self.min_shot_duration:
                boundaries.append(cut)
        boundaries.append(end)
        return boundaries
        
    def build_shot_plan(
        self, 
        clip: ClipCandidate, 
        face_tracks: List[Dict[str, Any]], 
        speech_intervals: List[Dict[str, float]],
        scene_cuts: List[float] = None
    ) -> List[VirtualShot]:
        if scene_cuts is None: scene_cuts = []
        """
        Universal Content-Aware Strategy Router.
        Maps SemanticVisualTargets to a concrete FFmpeg shot plan.
        """
        shot_plan = []
        
        # Resolve tracker IDs within each source segment: IDs can be reused
        # across scenes and separate YOLO tracking runs.
        for segment in clip.source_segments:
            seg_start = segment.start_seconds
            seg_end = segment.end_seconds
            segment_tracks = [track for track in face_tracks
                              if seg_start <= track.get("time", -1) <= seg_end]
            track_classes, speaker_map, object_map, all_ids = (
                self._tracks_for_segment(segment_tracks))
            
            # Find semantic events that overlap with this segment
            relevant_events = []
            for ev in clip.visual_events or []:
                if ev.start_seconds < seg_end and ev.end_seconds > seg_start:
                    relevant_events.append(ev)
            if not relevant_events and clip.content_type.upper() in {
                    "PODCAST", "INTERVIEW", "CONVERSATION", "LATE_NIGHT"}:
                for beat in clip.micro_beats:
                    if beat.kind.upper() in {"REACTION", "SECOND_REACTION"} and \
                            beat.visual_subject and beat.start_seconds < seg_end and \
                            beat.end_seconds > seg_start:
                        relevant_events.append(SemanticVisualTarget(
                            start_seconds=beat.start_seconds,
                            end_seconds=beat.end_seconds,
                            visual_target_type="LISTENER_REACTION",
                            visual_target_description=beat.visual_subject,
                            importance=.9, preferred_composition="medium"))
            
            relevant_events.sort(key=lambda x: x.start_seconds)
            stable_scene_cuts = self._scene_boundaries(
                seg_start, seg_end, scene_cuts)[1:-1]
            
            # If no events, determine fallback based on content type
            if not relevant_events:
                comp = ("fit_with_background" if clip.content_type.upper() in {"SPORT", "SPORTS", "ACTION", "SCREEN_RECORDING", "GAMEPLAY"} else "medium")
                boundaries = [seg_start, *stable_scene_cuts, seg_end]
                for begin, end in zip(boundaries, boundaries[1:]):
                    if end > begin:
                        shot_plan.append(VirtualShot(begin, end, all_ids, comp))
                continue
                
            # Build timeline for this segment based on events
            current_time = seg_start
            
            # Inject scene cuts as boundaries
            boundaries = set([seg_start, seg_end])
            for cut in stable_scene_cuts:
                if seg_start < cut < seg_end:
                    boundaries.add(cut)
            for ev in relevant_events:
                boundaries.add(max(seg_start, ev.start_seconds))
                boundaries.add(min(seg_end, ev.end_seconds))
                
            boundaries = sorted(list(boundaries))
            
            # Create sub-segments
            current_time = seg_start
            
            for i in range(len(boundaries) - 1):
                b_start = boundaries[i]
                b_end = boundaries[i+1]
                
                if b_end <= b_start:
                    continue
                    
                # Find matching event
                covering = [ev for ev in relevant_events
                            if ev.start_seconds <= b_start and ev.end_seconds >= b_end]
                active_ev = max(covering, key=lambda ev: ev.importance,
                                default=None)
                        
                if not active_ev:
                    comp = "medium"
                    shot_plan.append(VirtualShot(b_start, b_end, all_ids, comp))
                    continue
                    
                ev_start = b_start
                ev_end = b_end
                ev = active_ev

                visible_tracks = [track for track in segment_tracks
                                  if b_start - .25 <= track.get("time", -1) <= b_end + .25]
                if visible_tracks:
                    local_classes, local_speakers, local_objects, local_ids = (
                        self._tracks_for_segment(visible_tracks))
                    speaker_map = local_speakers
                    track_classes = local_classes
                    object_map = local_objects
                    all_ids = local_ids
                
                # Determine target tracks for this event
                target_ids = []
                object_untracked = False
                v_type = ev.visual_target_type.upper()
                desc = ev.visual_target_description.upper()
                
                # If target is a speaker, try to map to face ID
                if v_type in {"ACTIVE_SPEAKER", "REACTION", "LISTENER_REACTION"}:
                    if "SPEAKER_A" in desc and "SPEAKER_A" in speaker_map: target_ids.append(speaker_map["SPEAKER_A"])
                    elif "SPEAKER_B" in desc and "SPEAKER_B" in speaker_map: target_ids.append(speaker_map["SPEAKER_B"])
                    elif "SPEAKER_C" in desc and "SPEAKER_C" in speaker_map: target_ids.append(speaker_map["SPEAKER_C"])
                    else: target_ids = [t for t in all_ids if track_classes.get(t) == "person"] # Fallback to all people
                    if not target_ids: target_ids = all_ids
                elif v_type == "OBJECT" or v_type == "ACTION":
                    # Try to map object description to tracked object classes
                    found = False
                    for c_name, tids in object_map.items():
                        if c_name.lower() in desc.lower() or c_name.lower() in v_type.lower():
                            target_ids.extend(tids)
                            found = True
                    if not found:
                        target_ids = []
                        object_untracked = True
                elif v_type == "SCREEN":
                    target_ids = all_ids
                else:
                    target_ids = all_ids
                    
                composition = ev.preferred_composition.lower()
                composition = {"screen_focus": "fit_with_background", "action_frame": "fit_with_background",
                               "context_frame": "fit_with_background", "two_shot": "medium",
                               "reaction_frame": "medium", "object_focus": "medium"}.get(composition, composition)
                
                # If we lose tracking for the object but it's not a screen/document, fallback to wide but prefer crop if possible
                if object_untracked and v_type in {"SCREEN", "DOCUMENT", "SLIDE"}:
                    composition = "fit_with_background"
                
                if clip.content_type == "SPORT" and v_type == "ACTION":
                    composition = "fit_with_background"
                
                if clip.content_type == "SCREEN_RECORDING" or v_type in {"SCREEN", "GAMEPLAY", "DOCUMENT", "SLIDE", "CHART", "OBJECT"}:
                    composition = "fit_with_background"

                zoom_intent = (ev.zoom_intent or "NONE").upper()
                if zoom_intent not in {"SUBTLE_IN", "SUBTLE_OUT"} or composition == "fit_with_background":
                    zoom_intent = "NONE"
                
                dur = ev_end - ev_start
                # Pacing rules (skip if it crosses a hard scene cut!)
                is_scene_cut = b_start in stable_scene_cuts
                
                if (dur < self.min_shot_duration and v_type != "REACTION" and
                        shot_plan and not is_scene_cut and
                        shot_plan[-1].start >= seg_start and
                        abs(shot_plan[-1].end - b_start) < .01):
                    # Merge with previous if it's too short, BUT ONLY if it's not crossing a real scene cut
                    shot_plan[-1].end = ev_end
                else:
                    shot_plan.append(VirtualShot(ev_start, ev_end, target_ids,
                                                 composition, ev.visual_target_description,
                                                 zoom_intent))
                
        return shot_plan
