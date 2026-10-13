import os
import sys
import time

# Increase limit for hallucinated large unquoted numbers in JSON
sys.set_int_max_str_digits(100000)
from pathlib import Path
from google import genai
from google.genai import types
import json
from app.config import config
from app.models.schemas import GeminiAnalysisResponse

from app.services.gemini_schemas import analysis_schema
from app.services.gemini_requests import GenerationSession, transient_failure, failure_category

PROMPT_VERSION = "auto-editor-v7-window-context"

EDITORIAL_PROMPT_TEMPLATE = """This is STAGE A, a broad and economical source scan. You are a professional short-form VIDEO EDITOR. WATCH THE VISUAL CONTENT. The transcript is only one source of evidence. Analyze the video, speech, sound, actions, reactions, objects, scene changes, and what is visible while each line is spoken. Preserve truth, coherence, payoff and emotional rhythm. Do not act as a transcript summarizer or quote extractor. Leave detailed micro_beats, importance_curve, hook_variants and effect_events empty in this broad pass; a focused pass will inspect promising windows.

Write a concise, specific, and highly engaging title for each actual moment in the language spoken in the video. Name the visible action or central point; avoid generic Highlights.

Return at most {clip_count} genuinely strong standalone Shorts. Search broadly and discover DISTINCT moments spaced across the ENTIRE source timeline (do not cluster them all in one section). {recall_guidance} The usual duration range is {min_seconds}-{max_seconds} seconds, but use only the time needed for hook, context, development and payoff.

First identify timeline events and narrative relationships (setup/payoff, question/answer, claim/proof, expectation/surprise, action/reaction, tension/release, problem/solution, joke/punchline, failure/reaction, challenge/result, before/after, conflict/resolution, mystery/reveal). Record who speaks, who listens, the visual change, emotional beat, object or action the viewer must notice, and whether a reaction matters more than the line. Note whether the image contradicts or strengthens the speech. Identify truthful reusable SOURCE visual inserts, but do not invent or fetch B-roll. Use multiple non-contiguous source_segments to edit out long dead air, irrelevant tangents, repeated sentences, and filler. Jump cuts MUST remain natural, truthful, and coherent. Preserve intentional pauses (e.g., PRESERVE_PAUSE_FOR_COMEDY). Never invent dialogue or events.

For each candidate consider 2-4 highly engaging hook structures internally, including a clickbaity cold open, a highly controversial or strong statement, or a visual action when supported. Prioritize clickbaity hooks that immediately grab attention. Explicitly include and emphasize famous people or recognizable entities (like MrBeast, celebrities, big brands) early in the video if they appear or are discussed. Report viable hook_variants with source segments, clarity, curiosity, first-second strength, continuity, payoff preservation and misleading risk. Choose the strongest safe plan; chronology is often best. Prefer an informative first frame, not a transition, blur or dead air. Identify payoff_timestamp, reaction_moments, a final button or completed reaction, required_context and a clean ending. Do not cut laughter or an emotional pause short. Estimate natural_loop_score without forcing a loop.

Score independent dimensions 0-10 in scores, including first 1/3/5 seconds, clarity, payoff, reaction, story, humor, visual interest, rewatch and shareability, context cost, dead time, confusion and weak ending. Scores are evidence-based estimates, not measured retention. Vary candidate categories and avoid duplicate stories or overlapping payoffs.

Every source segment and cut must have a concise editorial reason. Include cut_suggestions at speech, scene, reaction or action boundaries with type, reason and confidence; do not cut important words. Remove filler, repetition, irrelevant silence and waiting. Preserve pauses that create comedy, tension, reaction or emotion in preserve_pauses with reasons PRESERVE_PAUSE_FOR_COMEDY, PRESERVE_PAUSE_FOR_TENSION, PRESERVE_PAUSE_FOR_REACTION or PRESERVE_PAUSE_FOR_EMOTION. Do not cut words or reactions short. Choose pacing_mode from HYPER, FAST, ENERGETIC, CONVERSATIONAL, STORY, TENSION, EMOTIONAL, COMEDIC, EDUCATIONAL, VISUAL. Never apply fast cuts universally.

Visual events must answer WHAT SHOULD THE VIEWER LOOK AT, and WHY NOW? The subject may be the active speaker, the listener's reaction, two people, hands, product, object, screen, gameplay or full scene. For interviews, identify interruption, overlap, shared reaction and when the listener is more valuable than the speaker. An important object can take priority over a face. Use a contextual wide composition when both subjects matter. Choose virtual shot size from WIDE, MEDIUM, MEDIUM_CLOSE, CLOSE for a semantic reason, never by timer. A source scene cut may already provide enough variation. Prefer NO_EFFECT. Optional effect_events must name a semantic reason, confidence, intensity and source-time bounds. Use only subtle PUNCH_IN, REACTION_PUSH, SLOW_PUSH or IMPACT for observable beats; never decorate ordinary speech. No generic hook text. A brief context overlay must be truthful, source-supported and nonredundant with captions. Identify a few genuinely emphasized spoken words; Whisper will transcribe EDL audio later.

For promising moments, notice SETUP, QUESTION, CLAIM, ANTICIPATION, INTERRUPTION, REALIZATION, SURPRISE, JOKE_SETUP, PUNCHLINE, REACTION, SECOND_REACTION, AWKWARD_SILENCE, LAUGHTER, ESCALATION, REVEAL, PROOF, CONTRADICTION, FAIL, SUCCESS, VISUAL_CHANGE, IMPORTANT_OBJECT, EMOTIONAL_SHIFT, RESOLUTION and BUTTON. Report concise emotional_arc, dominant_emotion, peak_moment and a few importance_curve points (0-10 estimates, not fake measurements). Preserve the pause before a joke and reaction after it. Reject misleading isolated quotes. Say when payoff or visual evidence is uncertain. Every action must improve this specific moment.

For every non-contiguous source cut report cut_continuity: source boundaries, semantic/visual/audio continuity and misleading risk (0-10). If the cut hides necessary context or changes meaning, keep context or reject the candidate. Compress repeated setup and tangents only when the remaining story is coherent.

A brief context_overlay is optional only when its factual text is directly supported by a named source event, with evidence_event_id, and cannot be conveyed efficiently by source footage. Prefer keeping real source context. Never invent timing or a claim.

Reject moments that rely on celebrity alone, require too much outside context, start slowly, repeat a previous idea, are merely loud, or end before resolution. Do not manufacture hooks, context, dialogue, reactions, effects or tension.

Categories may include FUNNY, JOKE, BANTER, AWKWARD, REACTION, FAIL, UNEXPECTED, ABSURD, CHAOTIC, WHOLESOME, TENSION, REVEAL, INSIGHT, EDUCATIONAL, STORY, OPINION, DEBATE, CONTROVERSIAL_STATEMENT, SURPRISING, EMOTIONAL, DEMONSTRATION, ACTION and VISUAL_MOMENT. Use only categories supported by the actual content.

Local scene, speech and silence signals (supporting evidence, not semantic truth): {local_analysis}
Additional guidance: {editorial_guidance}
Return only data conforming to the supplied schema.
"""

SUPPLEMENTAL_CONTEXT_VERSION = 2


class GeminiService:
    def __init__(self):
        self.api_key = config.GEMINI_API_KEY
        self.model_name = config.GEMINI_MODEL
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
            from app.services.gemini_requests import bound_generation_transport
            bound_generation_transport(self.client)
        else:
            self.client = None

    def _generate_content(self, *, model, contents, config=None, phase="analysis"):
        self._phase = phase
        if not hasattr(self, "_generation_session"):
            self._generation_session = GenerationSession(getattr(self, "on_request_event", None))
        response = self._generation_session.generate(
            self.client, self._model_chain(model), contents, config, phase)
        self._last_generation_model = self._generation_session.last_model
        return response

    def _parse_analysis(self, response, duration=0, is_discovery=False):
        try:
            if is_discovery:
                from app.models.schemas import GeminiDiscoveryResponse, EDLSegment, ClipCandidate, GeminiAnalysisResponse
                disc = GeminiDiscoveryResponse.model_validate_json(response.text)
                clips = []
                for rank, c in enumerate(disc.clips, 1):
                    clips.append(ClipCandidate(
                        title=c.title,
                        short_description=c.short_description,
                        content_type=c.content_type,
                        topic="Unknown",
                        rank=rank,
                        reason="",
                        categories=[],
                        context_dependency="NONE",
                        payoff_timestamp=None,
                        source_segments=[EDLSegment(start_seconds=c.start_seconds, end_seconds=c.end_seconds, reason="Discovery")],
                        hook_present=c.hook_present,
                        payoff_present=c.payoff_present,
                        standalone_context=c.standalone_context,
                    ))
                result = GeminiAnalysisResponse(source_summary=disc.source_summary, clips=clips)
            else:
                from app.models.schemas import GeminiAnalysisResponse
                result = GeminiAnalysisResponse.model_validate_json(response.text)
        except ValueError:
            self._generation_session.record(dict(phase=self._phase,
                model=self._last_generation_model, category="INVALID_STRUCTURED_OUTPUT",
                structured_output=False))
            raise
        from app.services.semantic_validation import timeline_audit
        audit = timeline_audit(result, duration) if duration > 0 else []
        self._generation_session.record(dict(phase=self._phase,
            model=self._last_generation_model, category="RESPONSE_VALIDATED",
            structured_output=True, candidates=len(result.clips), timeline=audit))
        callback = getattr(self, "on_semantic_result", None)
        if callback:
            callback(self._phase, result.model_dump(exclude_unset=True))
        return result

    @staticmethod
    def _model_chain(model):
        return config.gemini_models(model)

    def check_setup(self) -> dict:
        return {
            "configured": bool(self.api_key),
            "model": self.model_name
        }

    def test_connectivity(self) -> bool:
        if not self.client:
            return False
        self._generation_session = GenerationSession()
        try:
            response = self._generate_content(
                model=self.model_name,
                contents="Hello, reply with exactly 'OK'"
            )
            return "OK" in response.text
        except Exception:
            return False

    def upload_video(self, filepath: Path, on_retry=None):
        import os, time, random, mimetypes
        from google.genai import errors

        if not self.client:
            raise ValueError("Gemini API key not configured")

        # Verify file before upload
        if not filepath.exists():
            raise FileNotFoundError(f"Local file missing: {filepath}")
        if not filepath.is_file():
            raise ValueError(f"Path is not a regular file: {filepath}")

        size = filepath.stat().st_size
        if size == 0:
            raise ValueError(f"File size is 0: {filepath}")

        mime, _ = mimetypes.guess_type(str(filepath))
        if mime is None:
            mime = "video/mp4"

        max_attempts = 4
        attempt = 1

        while attempt <= max_attempts:
            print(f"Uploading {filepath.name} to Gemini (Attempt {attempt}, Size: {size}, MIME: {mime})...")
            try:
                video_file = self.client.files.upload(path=str(filepath))
                break
            except Exception as e:
                is_transient = False
                wait_seconds = 0

                if isinstance(e, errors.APIError):
                    code = getattr(e, 'code', None)
                    if code in (502, 503, 504, 429):
                        is_transient = True
                elif isinstance(e, (ConnectionError, TimeoutError)):
                    is_transient = True

                if is_transient and attempt < max_attempts:
                    wait_seconds = min(60, (2 ** attempt) * 5 + random.uniform(0, 2))
                    msg = f"Uploading analysis video to Gemini — temporary server error, retrying in {wait_seconds:.1f}s..."
                    print(msg)
                    if on_retry:
                        on_retry(msg)
                    time.sleep(wait_seconds)
                    attempt += 1
                else:
                    if is_transient:
                        print("Gemini video upload is temporarily unavailable after multiple attempts.")
                        raise RuntimeError("Gemini video upload is temporarily unavailable after multiple attempts.") from e
                    raise

        deadline = time.monotonic() + 180
        try:
            on_stage = getattr(self, "on_stage", None)
            if video_file.state == "PROCESSING" and on_stage:
                on_stage("WAITING_FOR_AI_FILE", "Waiting for AI video preparation")
            while video_file.state == "PROCESSING":
                if time.monotonic() >= deadline:
                    raise TimeoutError("GEMINI_FILE_PROCESSING_TIMEOUT")
                time.sleep(5)
                video_file = self.client.files.get(name=video_file.name)
            if video_file.state == "FAILED":
                raise ValueError("Video processing failed on Gemini")
            return video_file
        except Exception:
            self.delete_video(video_file.name)
            raise

    def delete_video(self, file_name: str):
        if self.client:
            try:
                self.client.files.delete(name=file_name)
            except Exception as e:
                print(f"Failed to delete remote file: {e}")


    def _run_target_discovery_passes(self, video_file, settings: dict,
                                     result: GeminiAnalysisResponse, schema: dict,
                                     model: str, checkpoint=None, on_checkpoint=None) -> GeminiAnalysisResponse:
        count_mode = settings.get("count_mode", "AUTO")
        if count_mode != "TARGET":
            return result

        required_discovery = settings.get("clip_count", 20)
        max_passes = 3 # 1 global + 3 supplemental = 4 max discovery passes
        duration = float(settings.get("source_duration") or 0)
        if duration <= 0:
            return result

        checkpoint = checkpoint or {}
        compatible = checkpoint.get("version") == SUPPLEMENTAL_CONTEXT_VERSION
        if compatible:
            result = GeminiAnalysisResponse.model_validate(checkpoint["result"])
        # Tag initial broad pass
        for c in result.clips:
            if not c.discovery_pass or c.discovery_pass == "GLOBAL":
                c.discovery_pass = "GLOBAL"
                c.discovery_window_start = 0.0
                c.discovery_window_end = duration

        from app.models.schemas import GeminiDiscoveryResponse, EDLSegment, ClipCandidate
        from google import genai
        from google.genai import types
        import json

        searched_windows = [tuple(w) for w in checkpoint.get("windows", [])] if compatible else []
        pass_index = 0
        for pass_index in range(len(searched_windows) + 1, max_passes + 1):
            valid_distinct_count = len(result.clips)
            if valid_distinct_count >= required_discovery:
                result.discovery_complete = True
                break

            # Determine under-covered region
            intervals = list(searched_windows)
            for c in result.clips:
                if c.source_segments:
                    start = min(s.start_seconds for s in c.source_segments)
                    end = max(s.end_seconds for s in c.source_segments)
                    intervals.append((start, end))
            intervals.sort()

            merged = []
            for it in intervals:
                if not merged:
                    merged.append(list(it))
                else:
                    if it[0] <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], it[1])
                    else:
                        merged.append(list(it))

            gaps = []
            curr = 0.0
            for m in merged:
                if m[0] - curr > 10.0:
                    gaps.append((curr, m[0]))
                curr = m[1]
            if duration - curr > 10.0:
                gaps.append((curr, duration))

            if not gaps:
                result.discovery_complete = True
                break

            gaps.sort(key=lambda x: x[1]-x[0], reverse=True)
            # Bound cost even when broad discovery found no candidates.
            start, end = gaps[0]
            target_gap = (start, min(end, start + 300.0))
            searched_windows.append(target_gap)

            existing_titles = ", ".join(f'"{c.title}"' for c in result.clips)

            focused_video = video_file
            if getattr(video_file, "uri", None):
                metadata = {
                    "start_offset": f"{target_gap[0]:.3f}s",
                    "end_offset": f"{target_gap[1]:.3f}s",
                }
                focused_video = types.Part(
                    file_data=types.FileData(
                        file_uri=video_file.uri,
                        mime_type=getattr(video_file, "mime_type", None) or "video/mp4"),
                    video_metadata=types.VideoMetadata(**metadata))

            prompt = EDITORIAL_PROMPT_TEMPLATE.format(
                clip_count=required_discovery - len(result.clips),
                recall_guidance="Find DISTINCT standalone moments.",
                min_seconds=settings.get("min_clip_seconds", 30),
                max_seconds=settings.get("max_clip_seconds", 60),
                editorial_guidance=settings.get("editorial_guidance") or "None",
                local_analysis=settings.get("local_analysis", "No local analysis provided."),
            )
            prompt += f"""

This is SUPPLEMENTAL pass {pass_index}. You MUST find NEW distinct stories.
The video context provided to you contains ONLY the source window {target_gap[0]:.1f}s to {target_gap[1]:.1f}s.
FULL_SOURCE_DURATION_SECONDS = {duration:.1f}
WINDOW_START_SOURCE_SECONDS = {target_gap[0]:.1f}
WINDOW_END_SOURCE_SECONDS = {target_gap[1]:.1f}
TIMESTAMP_COORDINATE_SYSTEM = ABSOLUTE_SOURCE_TIME_SECONDS

Even though you are watching a sliced window, ALL source_segments and event timestamps MUST BE ABSOLUTE SECONDS from the very beginning of the original video (0.0 to {duration:.1f}).
Do not output timestamps starting from 0.0 unless the event actually happens at absolute source time 0.0.
0 <= start < end <= {duration:.1f}
DO NOT return timestamps outside the requested discovery window {target_gap[0]:.1f}s to {target_gap[1]:.1f}s.
DO NOT use HH:MM:SS format. DO NOT encode MM:SS as a decimal/integer (e.g., do not write 3110 for 31:10).
DO NOT repeat these already discovered stories: {existing_titles}."""

            try:
                response = self._generate_content(
                    model=model,
                    contents=[focused_video, prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                    phase="broad_supplemental"
                )
                disc = GeminiDiscoveryResponse.model_validate_json(response.text)
                if hasattr(self, "_generation_session"):
                    self._generation_session.record(dict(phase="broad_supplemental", model=self._last_generation_model,
                        category="RESPONSE_VALIDATED", structured_output=True, candidates=len(disc.clips),
                        supplemental_pass=pass_index))
                new_clips = []
                for c in disc.clips:
                    is_duplicate = False
                    for existing in result.clips:
                        existing_start = min(s.start_seconds for s in existing.source_segments)
                        existing_end = max(s.end_seconds for s in existing.source_segments)
                        overlap = max(0, min(c.end_seconds, existing_end) - max(c.start_seconds, existing_start))
                        if overlap > 0.5 * (c.end_seconds - c.start_seconds):
                            is_duplicate = True
                            break
                    if is_duplicate:
                        continue

                    new_clip = ClipCandidate(
                        title=c.title,
                        short_description=c.short_description,
                        content_type=c.content_type,
                        topic="Unknown",
                        rank=1,
                        reason="",
                        categories=[],
                        context_dependency="NONE",
                        payoff_timestamp=None,
                        source_segments=[EDLSegment(start_seconds=c.start_seconds, end_seconds=c.end_seconds, reason="Discovery")],
                        hook_present=c.hook_present,
                        payoff_present=c.payoff_present,
                        standalone_context=c.standalone_context,
                        discovery_pass=f"SUPPLEMENTAL_{pass_index}",
                        discovery_window_start=target_gap[0],
                        discovery_window_end=target_gap[1]
                    )
                    new_clips.append(new_clip)

                # Candidate-level validation for supplemental pass
                from app.services.semantic_validation import timeline_audit
                temp_result = GeminiAnalysisResponse(source_summary="temp", clips=new_clips)
                supp_audit = timeline_audit(temp_result, duration, window=target_gap)
                print(f"\n--- SUPPLEMENTAL PASS {pass_index} ---")
                print(f"WINDOW: {target_gap[0]} to {target_gap[1]}")
                print(f"RAW_RETURNED: {len(new_clips)}")
                valid_supp = [c for c, a in zip(new_clips, supp_audit) if a["valid"]]
                print(f"VALID_FIRST_RESPONSE: {len(valid_supp)}")
                invalid_supp = [a for a in supp_audit if not a["valid"]]
                print(f"INVALID_FIRST_RESPONSE: {len(invalid_supp)}")
                for inv in invalid_supp:
                    print(f"INVALID CANDIDATE: {inv['title']} - {inv['invalid_segments']}")

                if invalid_supp:
                    print(f"REPAIR_ATTEMPTED: True")
                    repair_prompt = prompt + f"\n\nThe previous supplemental answer contained invalid candidates:\n"
                    repair_prompt += json.dumps(invalid_supp) + "\n"
                    repair_prompt += f"\nRe-watch and return ONLY a corrected complete response for these specific invalid candidates. "
                    repair_prompt += f"\n\nThe video context provided to you contains ONLY the source window {target_gap[0]:.1f}s to {target_gap[1]:.1f}s."
                    repair_prompt += f"All timestamps MUST be SECONDS from SOURCE START (0.0 to {duration:.1f}). "
                    repair_prompt += f"Do NOT use HH:MM:SS format and do NOT encode MM:SS as a number. "
                    repair_prompt += f"Return an empty clips array if they cannot be repaired."

                    repair_response = self._generate_content(
                        model=model, phase=f"supp_timeline_repair_{pass_index}", contents=[focused_video, repair_prompt],
                        config=types.GenerateContentConfig(response_mime_type="application/json",
                                                            response_schema=schema))
                    repair_disc = GeminiDiscoveryResponse.model_validate_json(repair_response.text)
                    print(f"REPAIR_RETURNED: {len(repair_disc.clips)}")
                    repaired_clips = []
                    for c in repair_disc.clips:
                        repaired_clips.append(ClipCandidate(
                            title=c.title, short_description=c.short_description, content_type=c.content_type, topic="Unknown", rank=1, reason="", categories=[], context_dependency="NONE",
                            payoff_timestamp=None,
                            source_segments=[EDLSegment(start_seconds=c.start_seconds, end_seconds=c.end_seconds, reason="Discovery")],
                            hook_present=c.hook_present, payoff_present=c.payoff_present, standalone_context=c.standalone_context,
                            discovery_pass=f"SUPPLEMENTAL_{pass_index}_REPAIR", discovery_window_start=target_gap[0], discovery_window_end=target_gap[1]
                        ))

                    temp_repair_result = GeminiAnalysisResponse(source_summary="temp", clips=repaired_clips)
                    repair_supp_audit = timeline_audit(temp_repair_result, duration, window=target_gap)
                    valid_repaired_supp = [c for c, a in zip(repaired_clips, repair_supp_audit) if a["valid"]]
                    print(f"VALID_AFTER_REPAIR: {len(valid_repaired_supp)}")
                    print(f"INVALID_AFTER_REPAIR: {len(repaired_clips) - len(valid_repaired_supp)}")
                    valid_supp.extend(valid_repaired_supp)

                result.clips.extend(valid_supp)
                if on_checkpoint:
                    on_checkpoint("SUPPLEMENTAL_COMPLETE", {"supplemental_checkpoint": {
                        "version": SUPPLEMENTAL_CONTEXT_VERSION, "windows": searched_windows,
                        "result": result.model_dump()}})
            except Exception:
                # Preserve the broad checkpoint; let the worker report provider failure.
                raise

        # Stable unique ranks are also the critic/render lookup keys.
        for rank, candidate in enumerate(result.clips, 1):
            candidate.rank = rank
        result.discovery_passes_completed = 1 + len(searched_windows)
        return result

    def _deepen_and_critique(self, video_file, result: GeminiAnalysisResponse,
                            schema: dict, model: str, duration: float, limit: int = 3) -> None:
        """One bounded visual pass for promising windows; one text-only critic pass."""
        if duration <= 0 or not result.clips:
            return
        from app.services.editorial_service import score_candidate
        promising = sorted(result.clips, key=score_candidate, reverse=True)[:limit]
        model = config.GEMINI_DEEP_MODEL or model
        for candidate in result.clips:
            if candidate not in promising and candidate.deep_analysis_status in (None, "NOT_REQUESTED"):
                candidate.deep_analysis_status = "BROAD_FALLBACK"
        for candidate in promising:
            if not candidate.source_segments:
                continue
            start = min(s.start_seconds for s in candidate.source_segments)
            end = max(s.end_seconds for s in candidate.source_segments)
            if start < 0 or end <= start or end > duration + .5:
                continue
            focused_prompt = (
                "WATCH the visual content as a professional editor. Inspect only "
                f"source time {max(0, start - 2):.2f}-{min(duration, end + 2):.2f}s. "
                "Return ONE candidate for the SAME moment, with precise micro_beats, "
                "emotional_arc, importance_curve, speaker/listener reaction shots, "
                "2-4 truthful hook_variants and optional semantic effect_events. "
                "Construct the shortest complete story: HOOK, CONTEXT, SETUP, DEVELOPMENT, "
                "TURN, PAYOFF, REACTION, BUTTON as supported. Compress repetition and tangents "
                "using non-contiguous source_segments, while keeping source dialogue truthful. "
                "Inspect the first three seconds for greetings, filler or empty frames; choose "
                "a stronger supported opening. Preserve comedy/emotional pauses and the ending. "
                "Report protected_regions as normalized source-frame boxes for faces, hands, "
                "products, game UI, score or text captions must not obscure. "
                "A reaction or preserved pause can be more important than dialogue. "
                "NO_EFFECT is valid. Keep absolute source timestamps. Do not invent "
                "facts or alter the meaning or chronology of speech. Original plan: "
                + json.dumps(candidate.model_dump(include={
                    "title", "story_arc", "source_segments", "payoff_timestamp",
                    "reaction_moments", "visual_events", "content_type"}),
                    ensure_ascii=False)[:12000]
            )
            prompt_min_ts = max(0, start - 2)
            prompt_max_ts = min(duration, end + 2)
            focused_prompt += f"\n\nFULL_SOURCE_DURATION_SECONDS = {duration:.1f}\nWINDOW_START_SOURCE_SECONDS = {prompt_min_ts:.1f}\nWINDOW_END_SOURCE_SECONDS = {prompt_max_ts:.1f}\nTIMESTAMP_COORDINATE_SYSTEM = ABSOLUTE_SOURCE_TIME_SECONDS\n"
            focused_prompt += f"Even though you are watching a sliced window, ALL source_segments and event timestamps MUST BE ABSOLUTE SECONDS from the very beginning of the original video (0.0 to {duration:.1f}). Do not output timestamps starting from 0.0 unless the event actually happens at absolute source time 0.0.\n"
            try:
                focused_video = video_file
                if getattr(video_file, "uri", None):
                    # Reuse the uploaded File while processing only this
                    # candidate window. No second upload or proxy is needed.
                    metadata = {
                        "start_offset": f"{max(0, start - 2):.3f}s",
                        "end_offset": f"{min(duration, end + 2):.3f}s",
                    }
                    if "fps" in types.VideoMetadata.model_fields:
                        metadata["fps"] = (3 if candidate.content_type.upper() in
                                           {"SPORT", "ACTION", "GAMEPLAY"} else 2)
                    focused_video = types.Part(
                        file_data=types.FileData(
                            file_uri=video_file.uri,
                            mime_type=getattr(video_file, "mime_type", None)
                            or "video/mp4"),
                        video_metadata=types.VideoMetadata(**metadata))
                response = self._generate_content(
                    model=model, phase="deep", contents=[focused_video, focused_prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", response_schema=schema))
                result.analysis_models["deep"] = self._last_generation_model
                deep = self._parse_analysis(response, duration)
                if not deep.clips:
                    candidate.deep_analysis_status = "BROAD_FALLBACK"
                    continue
                detail = deep.clips[0]
                from app.services.editorial_service import validate_final_edl
                from app.services.semantic_validation import timeline_audit
                detail_audit = timeline_audit(deep, duration, window=(prompt_min_ts, prompt_max_ts))
                if (any(not item["valid"] for item in detail_audit) or not detail.source_segments or validate_final_edl(detail, duration) or
                        any(seg.start_seconds < max(0, start - 2) or
                            seg.end_seconds > min(duration, end + 2)
                            for seg in detail.source_segments)):
                    candidate.deep_analysis_status = "BROAD_FALLBACK"
                    continue
                for field in (
                    "title", "short_description", "topic", "story_arc", "source_segments",
                    "removed_regions", "required_context", "preserve_pauses", "reaction_moments",
                    "payoff_present", "payoff_timestamp", "resolution_present", "ending_quality",
                    "intentional_unresolved_ending",
                    "hook_strategy", "pacing_mode", "caption_emphasis", "caption_motion", "scores",
                    "micro_beats", "emotional_arc", "dominant_emotion", "peak_moment",
                    "reaction_window", "importance_curve", "hook_variants", "natural_loop_score",
                    "effect_events", "first_frame_reason", "ending_reason", "cut_continuity",
                    "visual_events", "protected_regions", "context_overlay"):
                    value = getattr(detail, field)
                    if field in detail.model_fields_set and value is not None:
                        if field == "scores":
                            value = candidate.scores.model_copy(update=value.model_dump(exclude_unset=True))
                        setattr(candidate, field, value)
                candidate.deep_analysis_status = "COMPLETE"
                candidate.deep_pass_status = "COMPLETE"
                candidate.deep_pass_error_code = None
            except Exception as exc:
                candidate.deep_analysis_status = "BROAD_FALLBACK"
                candidate.deep_pass_status = "UNAVAILABLE" if transient_failure(exc) else "FAILED"
                candidate.deep_pass_error_code = failure_category(exc)
                import pydantic
                if isinstance(exc, pydantic.ValidationError):
                    candidate.deep_pass_error_code = "SCHEMA_VALIDATION_ERROR"
                    fields = [str(e.get('loc', [])) for e in exc.errors()]
                    print("Focused editorial pass schema validation failed: model=" + str(exc.title) + " fields=" + str(fields))
                else:
                    print(f"Focused editorial pass failed: {type(exc).__name__}")

        critic_schema = {"type": "OBJECT", "properties": {
            "reviews": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "rank": {"type": "INTEGER"}, "issue_code": {"type": "STRING"},
                "severity": {"type": "NUMBER"},
                "suggested_start": {"type": "NUMBER"},
                "suggested_end": {"type": "NUMBER"}},
                "required": ["rank", "issue_code", "severity"]}}},
            "required": ["reviews"]}
        compact = [item.model_dump(include={
            "rank", "story_arc", "source_segments", "micro_beats", "payoff_timestamp",
            "reaction_moments", "hook_strategy", "visual_events", "scores",
            "first_frame_reason", "ending_reason"}) for item in promising]
        try:
            response = self._generate_content(
                model=model, phase="critic", contents=(
                    "Critique these candidate edit plans using only the provided "
                    "structured evidence. Why might a viewer swipe? Is the opening "
                    "clear, payoff real, best reaction present, ending clean, cut "
                    "truthful, subject visible? Return NONE unless a meaningful issue "
                    "is supported. Suggest at most a 2-second first/last trim when "
                    "it cannot remove words, setup, payoff or reaction. "
                    + json.dumps(compact, ensure_ascii=False)[:28000]),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=critic_schema))
            reviews = json.loads(response.text).get("reviews", [])
            self._generation_session.record(dict(phase="critic", model=self._last_generation_model,
                category="RESPONSE_VALIDATED", structured_output=isinstance(reviews, list)))
            by_rank = {item.rank: item.model_copy(deep=True) for item in promising}
            for review in reviews[:limit]:
                candidate = by_rank.get(review.get("rank"))
                if not candidate or float(review.get("severity", 0)) < 7:
                    continue
                code = str(review.get("issue_code", ""))[:48]
                if code == "NONE":
                    continue
                candidate.critic_issue = code
                # A single bounded revision of an edge is allowed. The local
                # EDL validator still checks context and payoff downstream.
                segments = candidate.source_segments
                if code == "SLOW_START" and "suggested_start" in review:
                    new = float(review["suggested_start"])
                    if segments[0].start_seconds < new <= segments[0].start_seconds + 2 \
                            and new < segments[0].end_seconds - .5:
                        segments[0].start_seconds = new
                elif code == "LONG_TAIL" and "suggested_end" in review:
                    new = float(review["suggested_end"])
                    if segments[-1].end_seconds - 2 <= new < segments[-1].end_seconds \
                            and new > segments[-1].start_seconds + .5 \
                            and (candidate.payoff_timestamp is None or
                                 new > candidate.payoff_timestamp + .2) \
                            and all(new >= reaction.end_seconds
                                    for reaction in candidate.reaction_moments):
                        segments[-1].end_seconds = new
            # Commit critic changes only after the complete response was parsed.
            from app.services.editorial_service import validate_final_edl
            for candidate in promising:
                proposed = by_rank[candidate.rank]
                if not validate_final_edl(proposed, duration):
                    candidate.source_segments = proposed.source_segments
                    candidate.critic_issue = proposed.critic_issue
                candidate.critic_status = "COMPLETE"
                candidate.critic_error_code = None
        except Exception as exc:
            for candidate in promising:
                candidate.critic_status = "UNAVAILABLE" if transient_failure(exc) else "FAILED"
                candidate.critic_error_code = failure_category(exc)
            print(f"Editorial critic pass failed: {type(exc).__name__}")

    def map_source(self, video_file, settings: dict, models: list) -> dict:
        duration = float(settings.get("source_duration") or 0)
        schema = {"type": "OBJECT", "properties": {
            "summary": {"type": "STRING"},
            "people": {"type": "ARRAY", "items": {"type": "STRING"}},
            "topics": {"type": "ARRAY", "items": {"type": "STRING"}},
            "windows": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
                "story": {"type": "STRING"}, "visual_evidence": {"type": "STRING"},
                "audio_evidence": {"type": "STRING"}},
                "required": ["start", "end", "story", "visual_evidence", "audio_evidence"]}}},
            "required": ["summary", "people", "topics", "windows"]}

        from google.genai import types
        config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema)
        if not hasattr(self, "_generation_session"):
            self._generation_session = GenerationSession(getattr(self, "on_request_event", None))
        self._generation_session.source_duration = duration
        response = self._generation_session.generate(
            self.client, models, [video_file,


                "PASS A — YOU ARE WATCHING A VIDEO. Map the ENTIRE source, not just speech. "
                "Identify people, topics, story arcs and promising windows with questions/answers, "
                "jokes/reactions, actions/results, reveals, demonstrations or emotional turns. "
                "Use body language, objects, important on-screen text, screen changes, laughter "
                "and sound as evidence. Return at most 16 windows across the full video with "
                "absolute source seconds. Explain the actual observable story and evidence, "
                "without guessing motives or fame. This is a source map, not the final edit. "
                f"Source duration: {duration:.2f} seconds."],
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema),
            phase="source_map")
        self._last_generation_model = self._generation_session.last_model
        data = json.loads(response.text)
        import math
        data["windows"] = [window for window in data.get("windows", [])[:16]
                           if isinstance(window, dict) and
                           all(isinstance(window.get(k), (int, float)) and math.isfinite(window[k]) for k in ("start", "end"))
                           and 0 <= window["start"] < window["end"] <= duration]
        return data


    def compress_candidate(self, video_file, candidate, settings: dict) -> list:
        from app.services.gemini_schemas import compression_schema
        import json
        prompt = (
            "Watch this SAME candidate in the supplied video. Create a shorter truthful edit "
            f"of {settings.get('min_clip_seconds', 5)}-{settings.get('max_clip_seconds', 60)} seconds. "
            "Use chronological non-overlapping cuts strictly INSIDE the union of the original "
            "source ranges below, with absolute source seconds. No context expansion. Preserve "
            "the hook, required context, complete words, payoff and necessary reactions/pauses. "
            "Remove repetition and tangents, never invent or change meaning. The final segment MUST end at least 1 second AFTER the payoff timestamp. Preserve the original "
            "payoff timestamp and required context intervals. Return an empty source_segments array "
            "if no truthful complete shorter edit is possible.")

        prompt += f"\n\nCandidate Semantic Context:\nTitle: {candidate.title}\nDescription: {candidate.short_description}\n"
        prompt += f"Original Story Arc: {candidate.story_arc.model_dump_json() if candidate.story_arc else 'None'}\n"
        prompt += "Protected semantic evidence: " + json.dumps(candidate.model_dump(include={
            "payoff_timestamp", "required_context", "reaction_moments", "preserve_pauses"})) + "\n"
        prompt += "Original Source Segments:\n"
        min_ts = 0.0
        max_ts = 0.0
        if candidate.source_segments:
            min_ts = min(s.start_seconds for s in candidate.source_segments)
            max_ts = max(s.end_seconds for s in candidate.source_segments)
        for s in candidate.source_segments:
            prompt += f"  {s.start_seconds} -> {s.end_seconds}\n"

        duration = float(settings.get("source_duration", 0.0))
        prompt += f"\nFULL_SOURCE_DURATION_SECONDS = {duration:.1f}\nWINDOW_START_SOURCE_SECONDS = {min_ts:.1f}\nWINDOW_END_SOURCE_SECONDS = {max_ts:.1f}\nTIMESTAMP_COORDINATE_SYSTEM = ABSOLUTE_SOURCE_TIME_SECONDS\n"
        prompt += f"Even though you are watching a sliced window, ALL source_segments and event timestamps MUST BE ABSOLUTE SECONDS from the very beginning of the original video (0.0 to {duration:.1f}). Do not output timestamps starting from 0.0 unless the event actually happens at absolute source time 0.0.\n"


        models_to_try = config.gemini_models(self.model_name, task="standard")
        schema = compression_schema()
        focused_video = video_file
        if getattr(video_file, "uri", None) and candidate.source_segments:
            focused_video = types.Part(file_data=types.FileData(
                file_uri=video_file.uri, mime_type=getattr(video_file, "mime_type", None) or "video/mp4"),
                video_metadata=types.VideoMetadata(
                    start_offset=f"{min(s.start_seconds for s in candidate.source_segments):.3f}s",
                    end_offset=f"{max(s.end_seconds for s in candidate.source_segments):.3f}s"))

        response = self._generate_content(
            model=models_to_try[0], phase="compress_candidate", contents=[focused_video, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=schema))
        try:
            data = json.loads(response.text)
            from app.models.schemas import EDLSegment
            proposed = [EDLSegment(**seg) for seg in data["source_segments"]]
            self._generation_session.record(dict(phase="compress_candidate", model=self._last_generation_model,
                category="RESPONSE_VALIDATED", structured_output=True, candidate_rank=candidate.rank))
            callback = getattr(self, "on_semantic_result", None)
            if callback:
                callback("compression_proposal", {"rank": candidate.rank, "title": candidate.title,
                    "source_segments": [s.model_dump() for s in proposed]})
            return proposed
        except (ValueError, TypeError, KeyError):
            raise ValueError("INVALID_COMPRESSION_OUTPUT") from None

    def analyze_video(self, video_file, settings: dict, checkpoints: dict = None, on_checkpoint=None) -> GeminiAnalysisResponse:
        from app.services.semantic_validation import timeline_audit, InvalidSemanticTimeline
        checkpoints = checkpoints or {}
        self._generation_session = GenerationSession(getattr(self, "on_request_event", None))
        duration = float(settings.get("source_duration") or 0)
        self._generation_session.source_duration = duration
        requested_count = max(1, int(settings.get("clip_count", 5)))
        primary_count = min(requested_count, 5) if settings.get("exact_clip_count") else requested_count
        if settings.get("count_mode") == "TARGET":
            recall_guidance = (
                "This is a HIGH-RECALL discovery pass across the ENTIRE source timeline. "
                "Find DISTINCT standalone moments and distribute discovery across the full timeline. "
                "Return reserve candidates. Do not stop after finding a few strong moments. "
                "Avoid multiple candidates representing the same story. "
                "Do not reject a strong semantic story merely because its raw source span exceeds 60 seconds (long candidates will be compressed later)."
            )
        else:
            recall_guidance = "Two excellent clips beat five mediocre ones."

        prompt = EDITORIAL_PROMPT_TEMPLATE.format(
            clip_count=primary_count,
            recall_guidance=recall_guidance,
            min_seconds=settings.get("min_clip_seconds", 30),
            max_seconds=settings.get("max_clip_seconds", 60),
            editorial_guidance=settings.get("editorial_guidance") or "None",
            local_analysis=settings.get("local_analysis", "No local analysis provided.")
        )
        if duration > 0:
            prompt += (f"\nThe FULL source duration is {duration:.5f} seconds. "
                       "All timestamps MUST be absolute numeric SECONDS from source start, "
                       "never MMSS digits, milliseconds or final-output time. "
                       "For example 18:55 means 1135 seconds, NOT 1855. "
                       f"Every source segment must satisfy 0 <= start < end <= {duration:.5f}. "
                       "Check every payoff and interval against these bounds before returning.")

        models_to_try = config.gemini_models(self.model_name, task="standard")
        light_models = config.gemini_models(self.model_name, task="lightweight")

        response_schema_dict = analysis_schema("discovery") if settings.get("count_mode") == "TARGET" else analysis_schema("broad")

        source_map, map_warning, source_map_model = {}, None, None
        if settings.get("auto_edit"):
            if "source_map" in checkpoints:
                source_map = checkpoints["source_map"]
                source_map_model = checkpoints.get("source_map_model", self.model_name)
                prompt += "\nPASS B — discover distinct complete stories using this source map: " + json.dumps(source_map, ensure_ascii=False)[:20000]
            else:
                try:
                    source_map = self.map_source(video_file, settings, light_models)
                    source_map_model = self._last_generation_model
                    if on_checkpoint:
                        on_checkpoint("SOURCE_MAP_COMPLETE", {"source_map": source_map, "source_map_model": source_map_model})
                    prompt += "\nPASS B — discover distinct complete stories using this source map: " + json.dumps(source_map, ensure_ascii=False)[:20000]
                except Exception as exc:
                    map_warning = "SOURCE_MAP_UNAVAILABLE"
                    print("Source map unavailable; broad discovery will reuse the file. "
                          f"type={type(exc).__name__}; see phase=source_map diagnostics.")

        if "semantic_discovery" in checkpoints:
            print("\n--- DISCOVERY RESUMED FROM CHECKPOINT ---")
            result = GeminiAnalysisResponse.model_validate(checkpoints["semantic_discovery"])
            audit = timeline_audit(result, duration) if duration > 0 else []
            model = checkpoints.get("discovery_model", self.model_name)
            self.model_name = model
        else:
            response = self._generate_content(
                model=models_to_try[0], phase="broad", contents=[video_file, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                    response_schema=response_schema_dict))
            model = self._last_generation_model
            self.model_name = model
            result = self._parse_analysis(response, duration, is_discovery=(settings.get("count_mode") == "TARGET"))
            from app.services.semantic_validation import timeline_audit, InvalidSemanticTimeline
            audit = timeline_audit(result, duration) if duration > 0 else []
        print("\n--- GLOBAL PASS ---")
        print(f"RAW_RETURNED: {len(result.clips)}")
        valid_clips = [c for c, a in zip(result.clips, audit) if a["valid"]]
        print(f"VALID_FIRST_RESPONSE: {len(valid_clips)}")
        invalid = [item for item in audit if not item["valid"]]
        print(f"INVALID_FIRST_RESPONSE: {len(invalid)}")
        for inv in invalid:
            print(f"INVALID CANDIDATE: {inv['title']} - {inv['invalid_segments']}")
        if invalid:
            print("REPAIR_ATTEMPTED: True")

            invalid_candidates = [c for c, a in zip(result.clips, audit) if not a["valid"]]
            min_ts = None
            max_ts = None
            for c in invalid_candidates:
                for s in c.source_segments:
                    if 0 <= s.start_seconds <= duration:
                        min_ts = min(min_ts, s.start_seconds) if min_ts is not None else s.start_seconds
                    if 0 <= s.end_seconds <= duration:
                        max_ts = max(max_ts, s.end_seconds) if max_ts is not None else s.end_seconds
                if c.payoff_present and c.payoff_timestamp is not None and 0 <= c.payoff_timestamp <= duration:
                    min_ts = min(min_ts, c.payoff_timestamp) if min_ts is not None else c.payoff_timestamp
                    max_ts = max(max_ts, c.payoff_timestamp) if max_ts is not None else c.payoff_timestamp

            repair_video = video_file
            repair_window = None
            repair_prompt = prompt
            if min_ts is not None and max_ts is not None:
                min_ts = max(0, min_ts - 120)
                max_ts = min(duration, max_ts + 120)
                if getattr(video_file, "uri", None) and (max_ts - min_ts < duration * 0.8):
                    repair_window = (min_ts, max_ts)
                    repair_video = types.Part(
                        file_data=types.FileData(
                            file_uri=video_file.uri,
                            mime_type=getattr(video_file, "mime_type", None) or "video/mp4"),
                        video_metadata=types.VideoMetadata(start_offset=f"{min_ts:.3f}s", end_offset=f"{max_ts:.3f}s"))
                    repair_prompt += f"\n\nThe video context provided to you contains ONLY the source window {min_ts:.1f}s to {max_ts:.1f}s.\n"
                    repair_prompt += f"FULL_SOURCE_DURATION_SECONDS = {duration:.1f}\nWINDOW_START_SOURCE_SECONDS = {min_ts:.1f}\nWINDOW_END_SOURCE_SECONDS = {max_ts:.1f}\nTIMESTAMP_COORDINATE_SYSTEM = ABSOLUTE_SOURCE_TIME_SECONDS\n"
                    repair_prompt += f"Even though you are watching a sliced window, ALL source_segments and event timestamps MUST BE ABSOLUTE SECONDS from the very beginning of the original video (0.0 to {duration:.1f}). Do not output timestamps starting from 0.0 unless the event actually happens at absolute source time 0.0.\n"

            # One repair against the SAME remote video; no new upload and no
            # guessed MMSS conversion or local clamping of hallucinated times.
            repair_prompt += f"\n\nThe previous answer contained invalid candidates with timestamps outside the {duration:.1f}-second video bounds or formatted as MM:SS (e.g. 3110 instead of 1870):\n"
            repair_prompt += json.dumps(invalid) + "\n"
            repair_prompt += f"\nRe-watch and return ONLY a corrected complete response for these specific invalid candidates. "
            repair_prompt += f"All timestamps MUST be SECONDS from SOURCE START (0.0 to {duration:.1f}). "
            repair_prompt += f"Do NOT use HH:MM:SS format and do NOT encode MM:SS as a number. "
            repair_prompt += f"Preserve the semantic moment while correcting only invalid timeline data. "
            repair_prompt += f"Return an empty clips array if they cannot be repaired."

            response = self._generate_content(
                model=model, phase="broad_timeline_repair", contents=[repair_video, repair_prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                    response_schema=response_schema_dict))
            repair_result = self._parse_analysis(response, duration, is_discovery=(settings.get("count_mode") == "TARGET"))
            print(f"REPAIR_RETURNED: {len(repair_result.clips)}")
            model = self._last_generation_model

            repair_audit = timeline_audit(repair_result, duration, window=repair_window)
            repaired_valid_clips = [c for c, a in zip(repair_result.clips, repair_audit) if a["valid"]]
            print(f"VALID_AFTER_REPAIR: {len(repaired_valid_clips)}")
            print(f"INVALID_AFTER_REPAIR: {len(repair_result.clips) - len(repaired_valid_clips)}")

            # Combine originally valid clips with successfully repaired ones
            result.clips = valid_clips + repaired_valid_clips

            if not result.clips and invalid:
                # If everything failed and could not be repaired, raise the exception so it doesn't silently pass
                # if it's genuinely irreparable
                raise InvalidSemanticTimeline(repair_audit if repair_audit else invalid)
        if "semantic_discovery" not in checkpoints and on_checkpoint:
            on_checkpoint("SEMANTIC_DISCOVERY_COMPLETE", {
                "semantic_discovery": result.model_dump(),
                "timeline_audit": timeline_audit(result, duration),
                "discovery_model": model
            })

        if not result.clips:
            # A valid empty response may reflect an overly broad first pass,
            # especially when many clips were requested. One focused pass is
            # bounded; never manufacture weak moments to meet the target.
            focused_limit = max(1, min(primary_count, 8))
            focused_prompt = (
                prompt + "\nThe previous pass returned no clip candidates. "
                "Inspect the whole source once more for complete, factual "
                "moments with their own setup and payoff. For this focused "
                f"pass, return at most {focused_limit} moments actually "
                "present in the video. "
                "A quiet but self-contained moment is acceptable. If there "
                "truly are none, return an empty clips array again."
            )
            try:
                focused_response = self._generate_content(
                    model=model,
                    phase="broad_empty_recovery",
                    contents=[video_file, focused_prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=response_schema_dict,
                    ),
                )
                focused_result = self._parse_analysis(focused_response, duration, is_discovery=(settings.get("count_mode") == "TARGET"))
                if duration > 0 and any(not item["valid"] for item in
                                        timeline_audit(focused_result, duration)):
                    raise InvalidSemanticTimeline(timeline_audit(focused_result, duration))
                if focused_result.clips:
                    result = focused_result
                    model = self._last_generation_model
            except Exception as focused_error:
                # The first response was valid. Preserve it and let the
                # pipeline report NO_SUITABLE_CLIPS rather than retrying
                # every model for an optional second pass.
                print("Focused candidate pass failed: "
                      f"{type(focused_error).__name__}")
        result = self._run_target_discovery_passes(
            video_file, settings, result, response_schema_dict, model,
            checkpoint=checkpoints.get("supplemental_checkpoint"), on_checkpoint=on_checkpoint)
        on_stage = getattr(self, "on_stage", None)
        if on_stage:
            on_stage("BUILDING_CANDIDATES", "Refining candidate stories and edits")
        if not settings.get("exact_clip_count"):
            self._deepen_and_critique(
                video_file, result, analysis_schema("deep"), model,
                float(settings.get("source_duration") or 0),
                limit=config.AUTO_EDIT_DEEP_CANDIDATES if settings.get("auto_edit") else 3)
        result.source_map = source_map
        result.analysis_models["broad"] = model
        if source_map:
            result.analysis_models["source_map"] = source_map_model
        if map_warning:
            result.analysis_warnings.append(map_warning)
        result.analysis_version = PROMPT_VERSION
        result.analysis_models = dict(result.analysis_models)
        return result
