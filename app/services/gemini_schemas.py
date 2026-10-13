"""Gemini wire schemas, separated by semantic phase after live API validation."""
from app.models.schemas import EditorialScores


def full_analysis_schema():
    response_schema_dict = {
        "type": "OBJECT",
        "properties": {
            "source_summary": {"type": "STRING"},
            "clips": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "rank": {"type": "INTEGER"},
                        "title": {"type": "STRING"},
                        "short_description": {"type": "STRING"},
                        "topic": {"type": "STRING"},
                        "content_type": {"type": "STRING"},
                        "story_arc": {
                            "type": "OBJECT",
                            "properties": {
                                "hook": {"type": "STRING"},
                                "setup": {"type": "STRING"},
                                "payoff": {"type": "STRING"}
                            },
                            "required": ["hook", "setup", "payoff"]
                        },
                        "reason": {"type": "STRING"},
                        "categories": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        },
                        "hook_strength": {"type": "INTEGER"},
                        "standalone_clarity": {"type": "INTEGER"},
                        "entertainment_value": {"type": "INTEGER"},
                        "humor_strength": {"type": "INTEGER"},
                        "surprise": {"type": "INTEGER"},
                        "emotional_strength": {"type": "INTEGER"},
                        "information_value": {"type": "INTEGER"},
                        "story_value": {"type": "INTEGER"},
                        "visual_interest": {"type": "INTEGER"},
                        "reaction_value": {"type": "INTEGER"},
                        "payoff_strength": {"type": "INTEGER"},
                        "editability": {"type": "INTEGER"},
                        "overall_score": {"type": "INTEGER"},
                        "context_dependency": {"type": "STRING"},
                        "cut_notes": {"type": "STRING"},
                        "source_segments": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "start_seconds": {"type": "NUMBER"},
                                    "end_seconds": {"type": "NUMBER"},
                                    "reason": {"type": "STRING"}
                                },
                                "required": ["start_seconds", "end_seconds"]
                            }
                        },
                        "removed_regions": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "start_seconds": {"type": "NUMBER"},
                                    "end_seconds": {"type": "NUMBER"},
                                    "reason": {"type": "STRING"}
                                },
                                "required": ["start_seconds", "end_seconds", "reason"]
                            }
                        },
                        "visual_events": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "start_seconds": {"type": "NUMBER"},
                                    "end_seconds": {"type": "NUMBER"},
                                    "visual_target_type": {"type": "STRING"},
                                    "visual_target_description": {"type": "STRING"},
                                    "importance": {"type": "NUMBER"},
                                    "secondary_target": {"type": "STRING"},
                                    "preferred_composition": {"type": "STRING"},
                                    "zoom_intent": {"type": "STRING"}
                                },
                                "required": ["start_seconds", "end_seconds", "visual_target_type", "visual_target_description", "importance", "preferred_composition"]
                            }
                        },
                        "subtitles": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "start_seconds": {"type": "NUMBER"},
                                    "end_seconds": {"type": "NUMBER"},
                                    "text": {"type": "STRING"}
                                },
                                "required": ["start_seconds", "end_seconds", "text"]
                            }
                        }
                    },
                    "required": [
                        "rank", "title", "short_description", "topic", "content_type",
                        "reason", "categories", "context_dependency", "source_segments"
                    ]
                }
            }
        },
        "required": ["source_summary", "clips"]
    }

    interval = {"type": "OBJECT", "properties": {
        "start_seconds": {"type": "NUMBER"},
        "end_seconds": {"type": "NUMBER"},
        "reason": {"type": "STRING"}},
        "required": ["start_seconds", "end_seconds"]}
    clip_properties = response_schema_dict["properties"]["clips"]["items"]["properties"]
    clip_properties["source_segments"]["items"]["required"] = [
        "start_seconds", "end_seconds", "reason"]
    clip_properties.update({
        "scores": {"type": "OBJECT", "properties": {
            name: {"type": "NUMBER"} for name in EditorialScores.model_fields}},
        "required_context": {"type": "ARRAY", "items": interval},
        "preserve_pauses": {"type": "ARRAY", "items": interval},
        "reaction_moments": {"type": "ARRAY", "items": interval},
        "hook_strategy": {"type": "STRING"},
        "pacing_mode": {"type": "STRING"},
        "payoff_present": {"type": "BOOLEAN"},
        "payoff_timestamp": {"type": "NUMBER"},
        "resolution_present": {"type": "BOOLEAN"},
        "ending_quality": {"type": "NUMBER"},
        "intentional_unresolved_ending": {"type": "BOOLEAN"},
        "caption_emphasis": {"type": "ARRAY", "items": {"type": "STRING"}},
        "caption_motion": {"type": "STRING"},
        "cut_suggestions": {"type": "ARRAY", "items": {
            "type": "OBJECT", "properties": {
                "time": {"type": "NUMBER"}, "type": {"type": "STRING"},
                "reason": {"type": "STRING"}, "confidence": {"type": "NUMBER"}},
            "required": ["time", "type", "reason", "confidence"]}},
        "context_overlay": {"type": "OBJECT", "properties": {
            "text": {"type": "STRING"},
            "evidence_event_id": {"type": "STRING"},
            "start_seconds": {"type": "NUMBER"},
            "end_seconds": {"type": "NUMBER"}},
            "required": ["text", "evidence_event_id",
                         "start_seconds", "end_seconds"]},
        "micro_beats": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "start_seconds": {"type": "NUMBER"}, "end_seconds": {"type": "NUMBER"},
            "kind": {"type": "STRING"}, "evidence": {"type": "STRING"},
            "speaker": {"type": "STRING"}, "visual_subject": {"type": "STRING"},
            "emotion": {"type": "STRING"}},
            "required": ["start_seconds", "end_seconds", "kind"]}},
        "emotional_arc": {"type": "ARRAY", "items": {"type": "STRING"}},
        "dominant_emotion": {"type": "STRING"},
        "peak_moment": {"type": "NUMBER"},
        "reaction_window": interval,
        "importance_curve": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "source_time": {"type": "NUMBER"},
            **{name: {"type": "NUMBER"} for name in (
                "semantic_importance", "visual_interest", "emotional_intensity",
                "motion_energy", "reaction_strength", "surprise", "comedic_value")}},
            "required": ["source_time"]}},
        "hook_variants": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "strategy": {"type": "STRING"},
            "source_segments": {"type": "ARRAY", "items": interval},
            **{name: {"type": "NUMBER"} for name in (
                "first_1s", "first_3s", "clarity", "curiosity", "misleading_risk",
                "payoff_preservation", "continuity")}},
            "required": ["strategy", "source_segments"]}},
        "natural_loop_score": {"type": "NUMBER"},
        "effect_events": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "effect_type": {"type": "STRING"},
            "start_seconds": {"type": "NUMBER"},
            "end_seconds": {"type": "NUMBER"},
            "reason_code": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
            "intensity": {"type": "NUMBER"},
            "target": {"type": "STRING"}},
            "required": ["effect_type", "start_seconds", "end_seconds",
                         "reason_code", "confidence", "intensity"]}},
        "first_frame_reason": {"type": "STRING"},
        "ending_reason": {"type": "STRING"},
        "protected_regions": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            **{key: {"type": "NUMBER"} for key in ("start_seconds", "end_seconds", "x", "y", "width", "height")},
            "reason": {"type": "STRING"}}, "required": ["start_seconds", "end_seconds", "x", "y", "width", "height", "reason"]}},
        "cut_continuity": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "from_source_end": {"type": "NUMBER"},
            "to_source_start": {"type": "NUMBER"},
            "semantic_continuity": {"type": "NUMBER"},
            "visual_continuity": {"type": "NUMBER"},
            "audio_continuity": {"type": "NUMBER"},
            "misleading_risk": {"type": "NUMBER"}},
            "required": ["from_source_end", "to_source_start",
                         "semantic_continuity", "visual_continuity",
                         "audio_continuity", "misleading_risk"]}},
    })
    response_schema_dict["properties"].update({
        "source_visual_inserts": {"type": "ARRAY", "items": {
            "type": "OBJECT", "properties": {
                "source_start": {"type": "NUMBER"},
                "source_end": {"type": "NUMBER"},
                "subject": {"type": "STRING"},
                "relevance": {"type": "STRING"},
                "visual_quality": {"type": "NUMBER"},
                "possible_use": {"type": "STRING"}},
            "required": ["source_start", "source_end", "subject"]}},
        "events": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "event_id": {"type": "STRING"}, "start_seconds": {"type": "NUMBER"},
            "end_seconds": {"type": "NUMBER"}, "kind": {"type": "STRING"},
            "description": {"type": "STRING"},
            "visual_subject": {"type": "STRING"}, "emotion": {"type": "STRING"},
            "speaker": {"type": "STRING"},
            "visible_subjects": {"type": "ARRAY", "items": {"type": "STRING"}},
            "action": {"type": "STRING"}, "reaction": {"type": "STRING"},
            "scene": {"type": "STRING"},
            "audio_cues": {"type": "ARRAY", "items": {"type": "STRING"}},
            "on_screen_text": {"type": "STRING"},
            "required_context_event_ids": {"type": "ARRAY", "items": {"type": "STRING"}}},
            "required": ["event_id", "start_seconds", "end_seconds", "kind", "description"]}},
        "narrative_relationships": {"type": "ARRAY", "items": {
            "type": "OBJECT", "properties": {
                "from_event_id": {"type": "STRING"},
                "to_event_id": {"type": "STRING"}, "kind": {"type": "STRING"}},
            "required": ["from_event_id", "to_event_id", "kind"]}},
    })

    return response_schema_dict


def analysis_schema(phase="broad"):

    if phase == "discovery":
        return {
            "type": "OBJECT",
            "properties": {
                "source_summary": {"type": "STRING", "description": "1-2 sentence global summary."},
                "clips": {
                    "type": "ARRAY",
                    "description": "Lightweight discovery candidates.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "title": {"type": "STRING"},
                            "short_description": {"type": "STRING"},
                            "start_seconds": {"type": "NUMBER"},
                            "end_seconds": {"type": "NUMBER"},
                            "content_type": {"type": "STRING"},
                            "hook_present": {"type": "BOOLEAN"},
                            "payoff_present": {"type": "BOOLEAN"},
                            "standalone_context": {"type": "BOOLEAN"},
                            "semantic_score": {"type": "NUMBER"}
                        },
                        "required": ["title", "short_description", "start_seconds", "end_seconds"]
                    }
                }
            },
            "required": ["source_summary", "clips"]
        }

    schema = full_analysis_schema()
    clip = schema["properties"]["clips"]["items"]
    if phase == "broad":
        fields = set(clip["required"]) | {"story_arc", "scores", "payoff_present", "payoff_timestamp",
            "resolution_present", "ending_quality", "intentional_unresolved_ending", "required_context",
            "preserve_pauses", "reaction_moments", "visual_events", "pacing_mode", "hook_strategy"}
    elif phase == "deep":
        fields = set(clip["properties"]) - {"subtitles", "hook_strength", "standalone_clarity",
            "entertainment_value", "humor_strength", "surprise", "emotional_strength", "information_value",
            "story_value", "visual_interest", "reaction_value", "payoff_strength", "editability",
            "overall_score", "scores"}
        schema["properties"] = {k:v for k,v in schema["properties"].items() if k in {"source_summary", "clips"}}
    else:
        raise ValueError("Unknown analysis phase")
    clip["properties"] = {k:v for k,v in clip["properties"].items() if k in fields}
    # These are consumed by the existing safety/quality selector. Optional fields
    # were legally omitted by the live model, leaving every payoff unknown.
    clip["required"] += ["story_arc", "payoff_present", "resolution_present",
                         "intentional_unresolved_ending", "ending_quality",
                         "required_context", "preserve_pauses", "reaction_moments", "visual_events"]
    if phase == "broad":
        clip["required"].append("scores")
        clip["properties"]["scores"]["required"] = list(clip["properties"]["scores"]["properties"])
    else:
        clip["required"] += ["micro_beats", "hook_variants", "protected_regions",
                             "effect_events", "first_frame_reason", "ending_reason"]
    return schema

def compression_schema():
    return {
        "type": "OBJECT",
        "properties": {
            "source_segments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_seconds": {"type": "NUMBER"},
                        "end_seconds": {"type": "NUMBER"},
                        "reason": {"type": "STRING"}
                    },
                    "required": ["start_seconds", "end_seconds"]
                }
            }
        },
        "required": ["source_segments"]
    }
