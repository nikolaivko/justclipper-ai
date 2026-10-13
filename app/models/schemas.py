from pydantic import BaseModel, Field
from typing import List, Optional

class EDLSegment(BaseModel):
    start_seconds: float
    end_seconds: float
    reason: Optional[str] = None

class RemovedRegion(BaseModel):
    start_seconds: float
    end_seconds: float
    reason: Optional[str] = None

class ProtectedVisualRegion(BaseModel):
    start_seconds: float
    end_seconds: float
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    reason: str = "important visual content"


class SemanticVisualTarget(BaseModel):
    start_seconds: float
    end_seconds: float
    visual_target_type: str
    visual_target_description: str
    importance: float
    secondary_target: Optional[str] = None
    preferred_composition: str
    zoom_intent: Optional[str] = None

class StoryArc(BaseModel):
    hook: str
    setup: str
    payoff: str

class ClipSubtitle(BaseModel):
    start_seconds: float
    end_seconds: float
    text: str

class NarrativeEvent(BaseModel):
    event_id: str
    start_seconds: float
    end_seconds: float
    kind: str
    description: str
    visual_subject: Optional[str] = None
    emotion: Optional[str] = None
    speaker: Optional[str] = None
    visible_subjects: List[str] = Field(default_factory=list)
    action: Optional[str] = None
    reaction: Optional[str] = None
    scene: Optional[str] = None
    audio_cues: List[str] = Field(default_factory=list)
    on_screen_text: Optional[str] = None
    required_context_event_ids: List[str] = Field(default_factory=list)

class NarrativeRelationship(BaseModel):
    from_event_id: str
    to_event_id: str
    kind: str

class CutDecision(BaseModel):
    time: float
    type: str
    reason: str
    confidence: float = 0.5

class ContextOverlay(BaseModel):
    text: str
    evidence_event_id: str
    start_seconds: float
    end_seconds: float

class MicroBeat(BaseModel):
    start_seconds: float
    end_seconds: float
    kind: str
    evidence: str = ""
    speaker: Optional[str] = None
    visual_subject: Optional[str] = None
    emotion: Optional[str] = None

class ImportancePoint(BaseModel):
    source_time: float
    semantic_importance: float = 0
    visual_interest: float = 0
    emotional_intensity: float = 0
    motion_energy: float = 0
    reaction_strength: float = 0
    surprise: float = 0
    comedic_value: float = 0

class HookVariant(BaseModel):
    strategy: str
    source_segments: List[EDLSegment] = Field(default_factory=list)
    first_1s: float = 0
    first_3s: float = 0
    clarity: float = 0
    curiosity: float = 0
    misleading_risk: float = 0
    payoff_preservation: float = 0
    continuity: float = 0

class EffectEvent(BaseModel):
    effect_type: str
    start_seconds: float
    end_seconds: float
    reason_code: str
    confidence: Optional[float] = None
    intensity: float = 0
    target: Optional[str] = None

class SourceVisualInsert(BaseModel):
    source_start: float
    source_end: float
    subject: str
    relevance: str = ""
    visual_quality: float = 0
    possible_use: str = ""

class CutContinuity(BaseModel):
    from_source_end: float
    to_source_start: float
    semantic_continuity: float
    visual_continuity: float
    audio_continuity: float
    misleading_risk: float

class EditorialScores(BaseModel):
    hook_strength: float = 0
    curiosity_gap: float = 0
    standalone_clarity: float = 0
    payoff_strength: float = 0
    surprise: float = 0
    humor_strength: float = 0
    emotional_strength: float = 0
    tension: float = 0
    conflict: float = 0
    reaction_value: float = 0
    information_value: float = 0
    story_value: float = 0
    visual_interest: float = 0
    visual_change: float = 0
    motion_energy: float = 0
    social_shareability: float = 0
    relatability: float = 0
    rewatch_potential: float = 0
    comment_potential: float = 0
    editability: float = 0
    context_cost: float = 0
    dead_time_penalty: float = 0
    redundancy_penalty: float = 0
    ending_strength: float = 0
    first_1s_strength: float = 0
    first_3s_strength: float = 0
    first_5s_strength: float = 0
    mid_clip_retention: float = 0
    payoff_retention: float = 0
    ending_retention: float = 0
    early_swipe_risk: float = 0
    confusion_risk: float = 0
    context_dependency: float = 0
    slow_start_penalty: float = 0
    dead_air_penalty: float = 0
    repetition_penalty: float = 0
    weak_ending_penalty: float = 0

class DiscoveryCandidate(BaseModel):
    title: str
    short_description: str
    start_seconds: float
    end_seconds: float
    content_type: str = "INTERVIEW"
    hook_present: bool | None = None
    payoff_present: bool | None = None
    standalone_context: bool | None = None
    semantic_score: float | None = None

class GeminiDiscoveryResponse(BaseModel):
    source_summary: str
    clips: list[DiscoveryCandidate]

class ClipCandidate(BaseModel):
    rank: int
    title: str
    short_description: str
    topic: str
    content_type: str
    story_arc: Optional[StoryArc] = None
    reason: str
    categories: List[str]
    hook_strength: Optional[int] = None
    standalone_clarity: Optional[int] = None
    entertainment_value: Optional[int] = None
    humor_strength: Optional[int] = None
    surprise: Optional[int] = None
    emotional_strength: Optional[int] = None
    information_value: Optional[int] = None
    story_value: Optional[int] = None
    visual_interest: Optional[int] = None
    reaction_value: Optional[int] = None
    payoff_strength: Optional[int] = None
    editability: Optional[int] = None
    overall_score: Optional[int] = None
    context_dependency: str
    cut_notes: Optional[str] = None
    

    # Discovery Provenance
    discovery_pass: str = "GLOBAL"
    discovery_window_start: float | None = None
    discovery_window_end: float | None = None

    # New EDL structure
    source_segments: List[EDLSegment]
    removed_regions: Optional[List[RemovedRegion]] = []
    visual_events: Optional[List[SemanticVisualTarget]] = []
    subtitles: Optional[List[ClipSubtitle]] = []
    scores: EditorialScores = Field(default_factory=EditorialScores)
    required_context: List[EDLSegment] = Field(default_factory=list)
    preserve_pauses: List[EDLSegment] = Field(default_factory=list)
    reaction_moments: List[EDLSegment] = Field(default_factory=list)
    hook_strategy: str = "NORMAL_CHRONOLOGICAL"
    pacing_mode: str = "CONVERSATIONAL"
    payoff_present: Optional[bool] = None
    payoff_timestamp: Optional[float] = None
    resolution_present: Optional[bool] = None
    ending_quality: Optional[float] = None
    intentional_unresolved_ending: bool = False
    caption_emphasis: List[str] = Field(default_factory=list)
    caption_motion: str = "APPEAR"
    editorial_score: Optional[float] = None
    decision_trace: List[str] = Field(default_factory=list)
    selection_origin: str = "GEMINI"
    cut_suggestions: List[CutDecision] = Field(default_factory=list)
    context_overlay: Optional[ContextOverlay] = None
    micro_beats: List[MicroBeat] = Field(default_factory=list)
    emotional_arc: List[str] = Field(default_factory=list)
    dominant_emotion: Optional[str] = None
    peak_moment: Optional[float] = None
    reaction_window: Optional[EDLSegment] = None
    importance_curve: List[ImportancePoint] = Field(default_factory=list)
    hook_variants: List[HookVariant] = Field(default_factory=list)
    natural_loop_score: Optional[float] = None
    effect_events: List[EffectEvent] = Field(default_factory=list)
    style_profile: str = "CLEAN_VIRAL"
    effect_budget: int = 0
    overediting_score: float = 0
    relative_quality: float = 0
    critic_issue: Optional[str] = None
    first_frame_reason: Optional[str] = None
    ending_reason: Optional[str] = None
    cut_continuity: List[CutContinuity] = Field(default_factory=list)
    protected_regions: List[ProtectedVisualRegion] = Field(default_factory=list)
    deep_analysis_status: str = "NOT_REQUESTED"
    deep_pass_status: Optional[str] = None
    deep_pass_error_code: Optional[str] = None
    critic_status: Optional[str] = None
    critic_error_code: Optional[str] = None
    quality_flags: List[str] = Field(default_factory=list)
    compression_attempted: bool = False
    compression_status: Optional[str] = None
    compression_error_code: Optional[str] = None
    semantic_source_segments: List[EDLSegment] = Field(default_factory=list)
    proposed_compressed_segments: List[EDLSegment] = Field(default_factory=list)
    duration_before_compression: Optional[float] = None
    duration_after_compression: Optional[float] = None

    hard_rejection_reason: Optional[str] = None

class GeminiAnalysisResponse(BaseModel):
    source_summary: str
    clips: List[ClipCandidate]
    events: List[NarrativeEvent] = Field(default_factory=list)
    narrative_relationships: List[NarrativeRelationship] = Field(default_factory=list)
    analysis_version: str = "v1"

    discovery_complete: bool = False
    discovery_passes_completed: int = 1

    source_visual_inserts: List[SourceVisualInsert] = Field(default_factory=list)
    source_map: dict = Field(default_factory=dict)
    analysis_models: dict = Field(default_factory=dict)
    analysis_warnings: List[str] = Field(default_factory=list)

class ShortPerformanceFeedback(BaseModel):
    """Optional observed metrics; never substituted for missing analytics."""
    youtube_video_id: str
    views: Optional[int] = None
    average_view_duration_seconds: Optional[float] = None
    average_percentage_viewed: Optional[float] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    retention_points: Optional[List[float]] = None

class YouTubeMetadata(BaseModel):
    title: str
    description: str
    hashtags: List[str]
    tags: List[str]
    source_title: Optional[str] = None
    source_channel: Optional[str] = None
    source_video_id: Optional[str] = None
    source_video_title: Optional[str] = None
    source_channel_name: Optional[str] = None
    source_channel_id: Optional[str] = None
    source_url: Optional[str] = None
    rights_status: Optional[str] = None
    rights_evidence: Optional[str] = None
    language: Optional[str] = None

class VideoMetadata(BaseModel):
    filename: str
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    sample_rate: Optional[str] = None
    file_size: int
    rotation: int = 0

class RenderSettings(BaseModel):
    auto_edit: bool = False
    clip_count: Optional[int] = None
    count_mode: str = "AUTO"
    target_clip_count: Optional[int] = None
    exact_clip_count: bool = False
    min_clip_seconds: int = 30
    max_clip_seconds: int = 60
    aspect_mode: str = "center_crop"
    output_resolution: str = "1080x1920"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    subtitles: bool = False
    editorial_guidance: Optional[str] = None

class RenderJobRequest(BaseModel):
    filename: str
    candidates: List[ClipCandidate]
    settings: RenderSettings



class SourceAsset(BaseModel):
    requested_clip_count: Optional[int] = None
    count_mode: str = "AUTO"
    source_id: str
    provider: str
    provider_video_id: str
    title: str
    source_channel: Optional[str] = None
    source_video_id: Optional[str] = None
    source_video_title: Optional[str] = None
    source_channel_name: Optional[str] = None
    source_channel_id: Optional[str] = None
    rights_evidence: Optional[str] = None
    source_visibility: Optional[str] = None
    source_path: str
    source_url: Optional[str] = None
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str
    rights_status: str
    acquisition_status: str
    processing_status: str
    created_at: float
    acquired_at: Optional[float] = None
    thumbnail_path: Optional[str] = None
    storage_status: str = "LOCAL"
    storage_lifecycle_version: int = 0
