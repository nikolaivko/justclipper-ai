# Failed-job forensic report: job_youtube_iZYPypBztB0

## Established cause

**INVALID_GEMINI_OUTPUT + CANDIDATE_VALIDATION_BUG (misclassification).** The provider returned a schema-valid candidate with source coordinates outside the 1822.58068-second source. The old pipeline accepted it, skipped deep video analysis, normalized every segment to zero duration, and then reported a content-quality failure. Broad prompting did not include the known full source duration or an explicit seconds-versus-MMSS constraint. No deterministic source-to-output mapping bug or threshold bug was found.

Broad result: **DEGRADED**. Valid JSON/Pydantic response: **YES**. Usable timestamp-valid semantic candidate: **NO**. Broad producing model saved in the job: `gemini-3.5-flash-lite`. Successful attempt number was not persisted. No local-only substitution: exact_clip_count=false and exceptions propagate.

## Historical request chronology and limits

1. Broad, gemini-3.5-flash-lite, attempt 1: HTTP 503, SERVICE_UNAVAILABLE (logged). The request implementation sleeps 2 seconds and retries the same model, up to three attempts before a fallback.
2. Broad eventually returned the schema-valid candidate preserved in the semantic cache. Same producing model is recorded in the job. Attempt 2 is the inference consistent with the single warning, but success HTTP status, attempt and raw transport response were not logged; therefore exact historical chronology cannot be certified.
3. Deep: **zero video requests** for this candidate. start=1855/end=2215 fails the pre-request bound check against duration+0.5. The old analysis_models.deep field was assigned before this check, so it did not prove a deep request occurred. Candidate status remains NOT_REQUESTED.
4. Critic: text-only request is reached by the code even when deep is skipped. Its exact success/attempt/response was not persisted. No critic_issue is present in the cache.
5. Final selection rejects NO_VALID_SEGMENTS. Worker reports ALL_CANDIDATES_REJECTED.

503 causal relation: **UNKNOWN**. Recovery to structured output is proven; a causal link between transient 503 and incorrect timestamps is not. No zero/default score degradation is evident.

Configured broad/deep/critic chain: gemini-3.5-flash-lite → gemini-3.8-flash (config/.env). Deep model override empty. The historical selected broad model is known; fallback execution is not logged and must not be asserted.

## Raw candidate and scores

| Field | Persisted value |
|---|---|
| rank | 1 |
| title | The Impossible Pool Shot Challenge |
| category | challenge |
| source segments | 1855–1940 s; 2110–2215 s |
| selected duration | 190 seconds (85+105) |
| source span | 360 seconds |
| payoff_timestamp | 2213.0 |
| entertainment_value | MISSING — no such score supplied |
| overall_score | MISSING — scoring stage not reached |
| editorial_score | MISSING in original; offline replay score=79.4 |
| deep_analysis_status | NOT_REQUESTED |
| hook_strength | 8.5 |
| curiosity_gap | 8.0 |
| standalone_clarity | 9.0 |
| payoff_strength | 9.5 |
| surprise | 9.0 |
| humor_strength | 7.5 |
| emotional_strength | 8.0 |
| tension | 8.5 |
| conflict | 6.0 |
| reaction_value | 9.5 |
| information_value | 4.0 |
| story_value | 8.5 |
| visual_interest | 9.0 |
| visual_change | 8.0 |
| motion_energy | 7.5 |
| social_shareability | 9.0 |
| relatability | 7.0 |
| rewatch_potential | 8.5 |
| comment_potential | 8.5 |
| editability | 9.0 |
| context_cost | 2.0 |
| dead_time_penalty | 1.0 |
| redundancy_penalty | 1.0 |
| ending_strength | 9.0 |
| first_1s_strength | 8.5 |
| first_3s_strength | 8.5 |
| first_5s_strength | 8.5 |
| mid_clip_retention | 8.0 |
| payoff_retention | 9.5 |
| ending_retention | 9.0 |
| early_swipe_risk | 2.0 |
| confusion_risk | 1.5 |
| context_dependency | 2.0 |
| slow_start_penalty | 1.5 |
| dead_air_penalty | 1.0 |
| repetition_penalty | 1.0 |
| weak_ending_penalty | 1.0 |

## Exact rejection and funnel

Candidate #1: **NO_VALID_SEGMENTS**. Both segment starts exceed source duration. `_normalize` clamps start/end to 1822.58068, leaving zero-length segments; minimum retained length is 0.25 seconds. Both are removed. No quality-score, payoff, dedup or EDL gate is subsequently evaluated. Offline score=79.4 exceeds the configured global minimum=45. The returned payoff 2213 and reaction/context intervals also lie outside the source. No automatic conversion of MMSS-looking values was performed.

| Stage | Count |
|---|---:|
| Raw narrative events | 1 (intro 0–5 s; not a separate story) |
| Broad story candidates | 1 |
| Deep requests / complete deep candidates | 0 / 0 |
| Timeline-valid | 0 |
| Quality-valid | 0 (none reached the quality gate) |
| Diversity/dedup | 0 (not reached) |
| Final EDL candidates | 0 |

Actual execution order is broad → optional deep → critic → timeline/quality/dedup. It is not the illustrative funnel order in the request. **Zero occurred at source-segment normalization**, before quality scoring.

## Gates and thresholds (unchanged)

| Gate | Definition/value | Original candidate / result |
|---|---|
| source bounds | _normalize: clamp to [0,duration] | both intervals wholly outside |
| minimum segment | end-start >=0.25 s | both become 0 s: rejected |
| payoff/resolution | false rejected unless intentional unresolved ending | both true; not reached |
| ending_quality | reject <3, global | 9; not reached |
| payoff in EDL | source start <= payoff <= end-0.2 s | 2213 outside source; not reached |
| cut continuity | semantic<5 or misleading>5 at matching cut±0.25 s | no supplied cuts; not reached |
| silence trim | >=1.5 s silence inside interval; preserve comedy/reactions | not reached |
| editorial floor | config.EDITORIAL_MIN_SCORE=45, global | offline 79.4; not reached |
| critic penalty | -15 for MISLEADING/MISSING_PAYOFF/UNCLEAR_OPENING | no critic_issue |
| dedup | >=65% shorter overlap, or same topic/payoff within3s, or same story | only one candidate; not reached |
| auto-edit length 5–60s | only AutoEditDirector.select | not applicable: auto_edit=false |
| requested length 30–60s | prompt guidance; strict filter only exact-count/auto-edit paths | current exact_clip_count=false |

Score weights remain in app/services/editorial_service.py (POSITIVE_WEIGHTS/PENALTY_WEIGHTS). Only explicitly supplied dimensions are included; no missing score is treated as zero in this candidate. Hook-variant choice thresholds (misleading<=2, continuity>=7, payoff preservation>=7) select a variant, not reject the candidate; no variants were supplied.

## Timeline and artifact audit

Source: 1822.58068 s, 3840×2160, 25fps, AV1, 1,219,320,011 bytes. Local data matches semantic cache exactly: 52 silences, 436 scene cuts, 53 speech intervals. Last cut=1819.76, last speech ends at1822.58068. No evidence of milliseconds or a trimmed proxy offset in local maps. Old proxy is absent; its exact duration can no longer be independently measured. The existing encoder uses full source without seek/time scaling.

Source and exact-hash-bound local analysis are reusable. Old semantic result is INVALID. Old proxy was unlinked and Gemini file deletion was attempted unconditionally in the worker finally block. Remote file ID/status was not persisted, so actual deletion cannot be remotely certified or reused by ID. Rebuilding proxy and one new upload is necessary for the live retry.

Evidence snapshot is under logs/candidate-audit/job_youtube_iZYPypBztB0-20261001-225129: original job, original semantic cache, source hardlink, metadata, registry, log and deterministic replay. The original semantic cache is preserved at its original key; corrected cache uses the -timeline-v1 suffix.

## Fix

Explicit source-second contract and full duration in broad prompt; source bounds validation before deep analysis; at most one corrective semantic request using the same uploaded file; AI_INVALID_TIMELINE on repeated invalid coordinates. Preserve per-candidate decisions and funnel, successful request attempts and raw semantic passes. Repair of an invalid cached semantic result reuses source-hash-bound local analysis; a matching complete proxy can be reused. No threshold reduction, local guessed timestamps or filler clips.

Truthful progress stages: UPLOADING_TO_AI → WAITING_FOR_AI_FILE → ANALYZING_WITH_AI → BUILDING_CANDIDATES → VALIDATING_CANDIDATES. AI stages have no invented moving fraction or unsupported ETA. Legacy queue status names are retained internally to avoid changes to acquisition/lifecycle handling.

## Live retry

Results appended after the real run; see the evidence folder for the full retry job, request ledger and stage history.

### Verification isolation incident

The first live retry was interrupted before any Gemini upload: the existing
`test_startup_reconciliation_failure_does_not_block_app` opened the app lifespan
without isolating DATA_ROOT or mocking `prepare_for_new_video`. The full suite
was running concurrently with live proxy preparation, and startup cleanup removed
the active job/source/proxy. The preserved source hardlink and original JSON
snapshots allowed restoration of the original source, metadata, job and registry
without downloading. No original source bytes were lost.

Added tests/conftest.py to bind all test application singletons to a unique
project-local DATA_ROOT before collection. Final tests run before the second live
retry. Runtime cleanup policy is unchanged. Attempt-1 logs are preserved.

Independent source ffprobe verification: video=1822.52 s, audio/container=1822.58068 s; both streams start at0. No nonzero source start offset was found. Final isolated full suite: **317 passed**, 14 dependency deprecation warnings, 28.45 s. Original job/cache bytes and source hardlink identity were verified unchanged after the isolated suite.

Additional proxy inventory: bench_old.mp4/test_proxy_unlimited.mp4 are15s;
test_proxy_vt.mp4/test_proxy_benchmark.mp4 are30s; test_cmd.mp4 and test_proxy.mp4
have no readable complete format. None is a reusable full-source proxy.

During the second retry, the existing hardware proxy encoder was terminated by
SIGKILL (exit-9) at approximately84%. Its existing software fallback then started.
The source of SIGKILL is unknown. No FFmpeg/dav1d binaries, encoding settings or
YouTube acquisition code were changed during this investigation. Pending proxy
progress and containment fixes from the preceding task remain in the worktree.
