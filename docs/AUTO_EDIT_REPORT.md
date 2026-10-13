# Auto Edit implementation audit

## Existing architecture

FastAPI persists jobs in JobStore; the single PipelineWorker reserves HeavyGate,
prepares a proxy, invokes GeminiService, validates candidates, and delegates to
ClipService. VisualDirector maps semantic intent to shots; editorial_effects
validates effects; timeline.py maps source time to output time; FFmpegService
renders and render_qc checks the real output. StorageLifecycle owns cleanup.

Already implemented: visual source analysis, narrative events/relationships,
micro-beats, emotional arcs, retention scores, hook variants, context/payoff and
reaction protection, non-contiguous EDLs, silence trimming, style/effect budgets,
YOLO object/person tracking, OpenCV decoding, SmoothedCameraman, phrase captions
from faster-whisper word timestamps, semantic/visual/transcription caches and
ffprobe plus sampled-frame QC. Scene changes currently use FFmpeg; there is no
PySceneDetect dependency. B-roll suggestions exist as source metadata, but the
renderer has no separate visual/audio insert lanes, so automatic B-roll insertion
is intentionally unsupported.

## Gaps found before Auto Edit changes

- Upload requires separate analysis, selection, render clicks; exact-count excerpts
  are enabled by default in the UI and may turn an AI outage into unrelated ranges.
- Broad mapping and candidate discovery are combined, without a persisted source map.
- Focused analysis is bounded to three candidates; its refined EDL, setup, payoff,
  pause and caption decisions are not copied into the actual candidate.
- Planning coordination is embedded in ClipService rather than a reusable director.
- Gameplay and product demonstrations share generic style profiles.
- Tracking exceptions abort a clip; uncertain target mapping may crop too tightly.
- Caption failures are silently downgraded to no captions; placement only considers
  face boxes and two fixed vertical positions. Whisper remains resident indefinitely.
- There is no compact per-output retention map or unified editorial trace.
- Model fallback names are repeated in two services instead of central config.

## Earlier fixes carried forward

Commit 76c4294 fixed atomic job/source/OAuth persistence, render admission races,
invalid retry reservation release, partial-render recovery, portrait crops and
silent source audio, sparse tracking interpolation, unsafe HTML interpolation,
and actual-video metadata recovery with placeholder publication blocking.
239 tests passed before the Auto Edit extension. Live metadata recovery was tested
on an existing unpublished output: the provider did not produce a title. A direct
Gemini diagnostic returned HTTP 503 for three models. No videos were published.

## Verification and limits

Implementation and final test/deployment results will be appended below.
Synthetic fixtures validate contracts and observable render properties; they do
not establish real-world editorial quality or parity with another product.

## Auto Edit delivered

- **Orchestration / UX:** AutoEditDirector coordinates the existing Gemini
  understanding, semantic selection, VisualDirector and effect validation. The
  existing worker, heavy reservation, storage checks and renderer remain in use.
  `POST /api/auto-edit/{job_id}` queues one source through render completion.
  Upload → Make Shorts is primary; manual settings remain in a collapsed section.
  Library processing uses the same Auto Edit settings. Results show style, editorial
  quality tier, preview/download/publish, warnings and concise editing reasons.
- **Hierarchical Gemini:** Auto Edit adds one source-map pass (people, topics,
  observable stories and source windows), then existing broad candidate discovery.
  Up to five promising windows get focused analysis using the same uploaded file,
  followed by one bounded critic call. The source map, actual model IDs, prompt
  version and warnings are persisted. A map/deep failure retains usable broad
  analysis; a complete outage fails truthfully instead of creating random excerpts.
- **Story / hook / payoff:** focused source_segments, removed regions, required
  setup, pauses, payoff, reactions, hook variants and caption intent now reach the
  actual candidate. Existing hook ranking, context insertion, duplicate suppression,
  dead-time trimming and effect budgets remain active. A reaction_window is retained
  as a reaction interval. Leading irrelevant silence is trimmed fully. Auto Edit
  only accepts source-supported payoff (or intentional unresolved ending), 5–60s
  complete stories above the configured quality floor. It never pads to a count.
- **Styles / camera:** added distinct PRODUCT_DEMO and GAMEPLAY_DYNAMIC profiles.
  Existing comedy/emotional restraint, podcast/story/education/action profiles remain.
  Empty/uncertain tracks use wider stable framing, not a guessed tight crop; speaker
  maps refresh from local observations. Semantic TWO_SHOT / SCREEN_FOCUS /
  ACTION_FRAME / CONTEXT_FRAME map safely to the renderer. Full object boxes are
  retained while person boxes still focus on upper bodies. Tracker exceptions no
  longer invalidate a render. Temporary captures/media close on failures.
- **Effects:** reused existing validated PUNCH_IN, PUNCH_OUT, SLOW_PUSH,
  REACTION_PUSH and IMPACT camera effects; retained confidence/intensity bounds,
  per-style budgets, scene-change reduction and collision checks. No extra decorative
  effects or arbitrary generated FFmpeg commands were introduced.
- **Captions:** CaptionDirector handles selective word emphasis, restrained motion,
  protected visual regions and caption positioning, including avoiding context text.
  Source captions are remapped through final EDL cuts. Typical phrase maximum is six
  words (four in fast modes), with existing two-line layout. Transcription is still
  restricted to final selected source intervals. Failures explicitly mark the output
  as missing captions; unavoidable placement risks request preview review.
- **Critic / trace:** the existing single critic revision remains bounded; refined
  candidates retain validated source bounds. Edit plans now include final-time camera
  events, micro-beats, descriptive retention map, risks, captions, protected pauses,
  context overlays and concise reason strings. These are editorial estimates, not
  measured retention or viral probabilities.
- **Cache / cost:** semantic keys include source hash, prompt/schema version, broad
  and configured deep models, Auto Edit mode and deep-pass limit. Caption/resolution
  changes do not invalidate source understanding. Source map adds one full-source
  request; deep passes inspect windows and reuse the uploaded File. Metadata recovery
  analyzes only a finished Short. Model IDs/fallback order are centralized and
  configurable; existing configured/default models are retained.
- **RAM:** Whisper imports/weights load lazily, then weights are released after the
  heavy job. YOLO remains local to tracking calls. OpenCV handles and temporary
  tracking files now close in finally blocks. No new resident model or frame cache.
- **QC / regressions:** deterministic EDL/shot/effect validation occurs before
  rendering; caption time validation and canonical remapping are explicit. Existing
  ffprobe, dimensions, stream, duration, black-frame and freeze checks remain.
  Synthetic API→worker→FFmpeg testing produces a real 1080×1920 H.264/AAC MP4 and
  confirms safe tracking/caption fallbacks without any YouTube publication.

## Known limits

A 503 outage was reproduced against the actual Gemini API during this work. A
bounded delayed metadata retry and safe provider error messages were added. An API
outage cannot be solved by inventing titles or stories. Previously published titles
are not automatically renamed; current OAuth scopes and cleaned local media do not
support that workflow. The existing manual exact-count mode remains available only
in advanced settings and marks fallback excerpts; Auto Edit never enables it.

The renderer still has no independent source B-roll/audio lanes. SOURCE_BROLL_INSERT
and CONTROLLED_SPEEDUP are therefore not enabled; context text can represent a
source-supported key number without fabricating one. Audio semantics are provided
by Gemini and local speech/silence signals, not a new dedicated laughter/applause
classifier. Caption protected regions are conservative estimates and cannot promise
pixel-perfect avoidance in every shot. The source map is bounded to 16 windows and
deep inspection to the configured strongest candidates; long sources may contain
additional moments not selected in one run.

No real source video has been rendered and visually inspected **after the Auto Edit
deployment**. Real-world editing quality and parity with any other product are
**NOT YET VERIFIED**. Deterministic tests prove behavior and render properties only.

## Final verification

- `venv/bin/python -m pytest -q --durations=3`: **261 passed**, 13 dependency
  deprecation warnings (Starlette TestClient / anyio; no test failures).
- `node --check app/ui/app.js`: passed.
- `git diff --check`: passed.
- Existing regressions cover Trends/acquisition/rights, Source Library, disk
  preflight, heavy locking, retention/cleanup, OAuth, Publish/Publish All, public
  upload default, metadata and attribution. No random source downloads, public
  uploads or unclassified legacy-data deletions were performed for Auto Edit.
- Deployment commit, push result, Railway status and live `/health` verification
  are reported in the delivery response after deployment.

### Files changed for Auto Edit

New: `app/services/auto_edit_director.py`, `app/services/caption_director.py`,
`tests/test_auto_edit.py`, `docs/AUTO_EDIT_REPORT.md`.

Updated: `.env.example`, `app/config.py`, `app/models/schemas.py`,
`app/api/routes.py`, `app/services/worker.py`, `app/services/gemini_service.py`,
`app/services/semantic_cache.py`, `app/services/editorial_service.py`,
`app/services/editorial_effects.py`, `app/services/visual_director.py`,
`app/services/video_analyzer.py`, `app/services/transcription_service.py`,
`app/services/clip_service.py`, `app/services/ffmpeg_service.py`,
`app/services/clip_metadata_service.py`, `app/ui/index.html`, `app/ui/app.js`,
`tests/test_editorial_quality.py`, `tests/test_metadata_recovery.py`,
`tests/test_phase2_editor.py`.
