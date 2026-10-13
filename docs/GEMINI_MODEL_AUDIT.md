# Gemini model / request compatibility audit

Verified locally 2026-09-30, starting from commit `2ddce20`. Scope: model selection,
`generateContent` requests, structured responses and per-analysis model health.
Files upload/retry implementation, local migration, acquisition, FFmpeg,
VisualDirector, Whisper and StorageLifecycle were not changed.

## 1. Previous configuration and provenance

| ID | Previous origin | Verification |
|---|---|---|
| `gemini-3.8-flash` | `app/config.py` primary/default fallback, `.env.example`, metadata/exact-count tests | Official stable ID; present in this key's catalog |
| `gemini-3.6-flash` | Local nonsecret `GEMINI_MODEL` override; default fallback list | Official stable ID; present in catalog |
| `gemini-3.7-flash` | Default fallback list | Official stable ID; present in catalog |
| `gemini-3.5-flash` | Default fallback list and error tests | Official stable ID; present in catalog |
| `gemini-3.5-flash-lite` | Default fallback list and error tests | Official stable ID; present in catalog; real video/JSON calls work |
| `gemini-3.1-pro-preview` | Error/zero-quota regression fixtures; not a default | Official preview ID; present in catalog; real text call returns 429, limit 0 |
| `gemini-2.5-flash` | Legacy-setting regression fixture | Official legacy model; not selected by current configuration |
| `gemini-2.5-pro` | Stale README setup instructions | Official legacy model; README corrected |

There was no separate `PRIMARY_ANALYSIS_MODEL` variable. Primary was `GEMINI_MODEL`.
`GEMINI_DEEP_MODEL` and `GEMINI_METADATA_MODEL` were empty: both inherited the chosen
analysis model. Source map used the first analysis model. There is no `config/`
directory. Original `scripts/` had no Gemini model IDs. Abstract `model-a/model-b`
unit-test fixtures are not production configuration.

**None of the three questioned 3.6/3.7/3.8 IDs was invented or a floating alias.**
They were unverified in the previous implementation, but are now corroborated by
the actual catalog and [official model list](https://ai.google.dev/gemini-api/docs/models).
2.5 models remain documented, with access restrictions for new projects; a legacy
test does not establish quota for this key.

## 2. Real catalog

The configured key successfully listed **61 models across all pages** using
`GET /v1beta/models`. SDK 0.3.0's `models.list()` targets `tunedModels` and lacks
`query_base`, so the diagnostic uses documented REST with the key in a header.
No key is printed. Full display names, descriptions, versions, token limits,
supported generation methods and advertised thinking flags are retained in the
ignored project-local `logs/gemini-model-catalog.json`. Catalog presence is not
proof of usable quota. It does not enumerate video/schema capabilities; those
were checked separately against documentation and live probes.

The complete catalog ID inventory is appended below.

## 3. Selected models

- Primary, source map, broad discovery: **`gemini-3.5-flash-lite`**.
- Deep analysis: inherits the successful primary by default; **`gemini-3.5-flash-lite`** in this verification.
- One default fallback: **`gemini-3.8-flash`**, officially compatible but capacity-limited during probes.
- Metadata: inherits the same primary unless explicitly configured.
- Pro preview is not in defaults: the real project has **zero free-tier quota** for it.
- Central configuration limits chains to three distinct, verified IDs. Unknown IDs
  cannot reach generation. A configured deep preference falls back to primary.
- The local `.env` primary was changed from 3.6 to 3.5 Flash-Lite without displaying
  or staging any secret. Existing running processes need a restart to read it.

The stronger 3.8 model can be selected for deep analysis through `GEMINI_DEEP_MODEL`
when capacity permits. It is not forced onto every candidate while unavailable.

## 4. Capability and live-probe matrix

| Candidate | Official text/video → text | Catalog generateContent | Files reference + small JSON | Full old schema | Actual project probe |
|---|---|---|---|---|---|
| 3.5 Flash-Lite | Yes | Yes | Verified | 400 | Operational; final broad/deep schemas accepted |
| 3.8 Flash | Yes | Yes | Documented; full ladder blocked at text probe | 400 on original schema experiment | Text-only 503; no claim of current usable capacity |
| 3.6 Flash | Yes | Yes | Source map with explicit File reference verified | Not probed | Initially text-only 503; later source map succeeds |
| 3.7 Flash | Yes | Yes | Documented, live video probe skipped after 503 | Not probed | Text-only 503 |
| 3.1 Pro preview | Yes | Yes | Documented, not live-verified for this key | Not probed | Text-only 429 with explicit limit 0 |

3.5 Flash is catalog/documentation verified but was not chosen or probed: adding
more fallback traffic was unnecessary once Flash-Lite worked.

| Request feature | Application use and verification |
|---|---|
| Text, video, Files API references | Explicit FileData verified on two tiny local MP4s and existing source |
| `response_mime_type` + `response_schema` | Real small JSON, broad schema and deep schema accepted |
| `response_json_schema` | Not used; unavailable in installed SDK 0.3.0's config model |
| System instruction | Small video probe accepted with factual-description instruction |
| Thinking config | Not sent; installed SDK lacks ThinkingConfig; model support alone does not make this SDK option usable |
| Media resolution | Not sent; SDK exposes the field, but this optional combination was not live-probed |
| Other generation parameters | Production semantic calls set only MIME/schema; no speculative thinking/temperature changes |
| Video start/end offsets | Explicit REST `videoMetadata` verified with selected candidate windows |

Sources: [Flash-Lite capabilities](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite),
[3.8 capabilities](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[video references and clipping](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding),
[structured output limitations](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

## 5. Exact request failures and fixes

### Empty video part

SDK **0.3.0** serializes a bare `types.File` passed in `contents` as
`{"parts":[{}]}`. Upload reaches ACTIVE, but the generation request does not contain
the video. A transport-capture regression proves this. `video_contents()` now
converts Files to `Part(file_data=FileData(file_uri=..., mime_type=...))`.
Upload code was not modified.

### 400 isolation

Controlled ladder on the same 2-second, 32,738-byte local test pattern:

1. Text `Return OK`: success on Flash-Lite.
2. Explicit video + question: success, describes color bars/time counter.
3. Video + system instruction: success.
4. Video + `{"summary": string}` schema: success, valid JSON.
5. Video + minimal editorial schema: success, valid candidate JSON.
6. Video + original complete Auto Edit schema: **400 INVALID_ARGUMENT**.
7. There are no additional production generation parameters beyond MIME/schema;
   the full-config stage is identical to stage 6.

Original schema: **10,198 bytes**, logical schema depth **8**. No recursive refs,
`$defs`, unions, `anyOf`, `oneOf`, tuple schemas, open maps, defaults or nullable
types are sent. It uses fixed objects, arrays and primitives; Pydantic application
models are not directly sent as the wire schema.

The combined schema is rejected, whereas both semantic subsets are accepted with
the same model/file/config. This establishes a **combined-schema compatibility /
complexity failure**, not an invalid model ID or video-upload error. Google's
message is only `Request contains an invalid argument.` It does **not** identify
a field or threshold; claiming a particular property, byte limit or depth limit
as the exact provider-internal cause would be unsupported.

The existing hierarchical passes now use:

- Source map: original small map schema.
- Broad discovery: **6,311 bytes**, depth 6; stories, evidence, scores, context,
  reactions, payoff, source segments, events and narrative links.
- Deep candidate windows: **6,900 bytes**, depth 8; precise EDL, hooks, micro-beats,
  protected regions, reactions, camera/effect intent, continuity and endings.
- Local composition and existing quality filters remain unchanged.

Shared full schema is retained for audit/negative probes, never sent by the live
Auto Edit path. Redundant legacy scalar scores are not requested alongside the
modern score object. Broad scores are preserved through deep merging. Required
payoff/evidence fields prevent valid-but-incomplete JSON from silently becoming
unknown-payoff candidates. `false` and empty arrays remain valid; no payoff is
invented locally to satisfy the selector. No extra semantic pass was added.

### Window compatibility

SDK 0.3.0 explicitly raises `ValueError: video_metadata parameter is not supported
in Google AI.` before making the HTTP call. A narrow adapter uses the same SDK's
authenticated transport for windowed `generateContent`, with documented
`videoMetadata.startOffset/endOffset`, and its normal response conversion / extra
field stripping. No dependency upgrade or Files-method patch was required.
Tests cover wire offsets and new server usage fields absent from old SDK models.

## 6. Source-map errors, retry policy and cost

The historical local log saved only `ServerError`; its exact original status/body
cannot be recovered retrospectively. Initial real 3.6/3.7/3.8 text probes returned
**503 UNAVAILABLE, high demand**. A later single 3.6 source-map probe succeeded,
confirming availability varies. The old source-map exception cannot honestly be
relabeled as a confirmed historical quota error.

Every failed request now logs phase, model, HTTP status, category and attempt,
without raw credentials or prompts. Source-map fallback to broad discovery remains.

- **400:** deterministic incompatibility; one attempt per model/config/input-kind.
  Same schema/config with a different prompt does not reset the block. A genuinely
  different schema or modality may still use that model.
- **429:** rate/quota category; explicit `limit: 0` is separately `ZERO_QUOTA`.
  No immediate retry; skip that model for the remainder of the analysis.
- **503:** capacity/service category, never inferred to be quota. At most three
  attempts, with two sleeps (2s, 4s); then disable that model for all later phases.
- Auth/not-found failures also stop repeated calls. Mixed failures preserve their
  distinct categories. Local validation/programming errors are not retried as overload.
- Health is in-memory, scoped to one analysis; the next analysis gets a fresh budget.
- Uploaded File is reused for fallback and all windows. No upload per model.
  Semantic cache version changes because old cache entries may describe absent video.

## 7. Real end-to-end results

The final tiny semantic test used existing `data/input/test_short.mp4`:
**539,388 bytes, 11.04s**. Upload → ACTIVE → explicit video reference → source map
→ broad candidate JSON → windowed deep JSON → critic completed. One candidate;
deep status `COMPLETE`. Responses identified people beside the sea, Greek scenery,
boats and a channel-promotion voiceover. This proves transport and semantic JSON
operation, not perfect grounding: the latest generated title included an unsupported
channel name. Editorial factual accuracy still needs review; no quality-parity claim.

Existing `job_youtube_4bZzhSl-wXU` source was present: **233,175,653 bytes,
976.329433s**. No proxy remained. The source was reused without downloading;
one remote upload was reused for subsequent checks. Source map and broad discovery
returned **five candidates**, and all five windowed deep requests completed.
The final required-field contract was checked using those cached candidate windows,
not by re-analyzing the entire source. Final selection outcome is recorded below.

These checks do not render or publish, and do not overwrite the user's existing job
state. Restart the existing local process before running the updated UI pipeline.

**Final existing-job outcome:** the final window-only retry produced four `COMPLETE`
deep candidates and one `BROAD_FALLBACK` after local EDL validation. No API failure
occurred. Applying the unchanged selector locally retained **zero eligible Shorts**:
weak ending, cut continuity risk, no resolution, excessive duration, and an
unconfirmed payoff prevented selection. The Gemini transport/candidate-generation
blocker is fixed; this source has **not** been demonstrated to produce a renderable
Short. The editorial gates were not bypassed and no larger-video retry was made
just to force a positive result. This is an explicit remaining editorial limitation,
outside this request-compatibility task.

## 8. Tests, containment and delivery

Full suite command: `TMPDIR="$PWD/data/temp" .venv/bin/python -m pytest tests/ -q
--basetemp=data/temp/pytest-gemini-release`.

Result: **293 passed, 14 deprecation warnings**, versus the 279-test baseline.

Regression coverage includes actual SDK wire conversion, catalog-verified chain,
400/429/503 distinctions, mixed failures, job circuit reset, no repeated dead model,
same File URI across fallback without upload, source-map safe diagnostics, actual
full/phase schema serialization, required semantic fields, window transport and
forward-compatible SDK response parsing.

Existing error-test expectations changed intentionally: two sleeps rather than a
sleep after the exhausted retry budget, one attempt for local programming errors,
and a two-model default chain. Error-only fixtures omit duration so they do not
accidentally count unrelated critic calls. Window regression now asserts actual
wire JSON rather than only a Python Part. No quality gate was weakened.

All diagnostic JSON, remote-file handles, frames and test artifacts are under
`PROJECT_ROOT/logs` or `PROJECT_ROOT/data/temp`. Circuit state is memory-only.
`scripts/audit-local-containment.sh` reports PASS; it notes an existing shared
Hugging Face cache outside the project, not a new artifact from this task.

No secrets, OAuth state, videos, models or caches belong in the commit. The pre-existing
untracked `patch_upload.py` and `patch_test_metadata.py` are left untouched.
Commit/push result is reported separately so this document does not self-reference
its commit hash. No Railway deploy command is authorized by this task.

## Complete catalog inventory

| Model ID | Display name | Advertised generation methods |
|---|---|---|
| `gemini-2.5-flash` | Gemini 2.5 Flash | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-2.5-pro` | Gemini 2.5 Pro | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-2.5-flash-preview-tts` | Gemini 2.5 Flash Preview TTS | countTokens, generateContent |
| `gemini-2.5-pro-preview-tts` | Gemini 2.5 Pro Preview TTS | countTokens, generateContent, batchGenerateContent |
| `gemma-4-26b-a4b-it` | Gemma 4 26B A4B IT | generateContent, countTokens |
| `gemma-4-31b-it` | Gemma 4 31B IT | generateContent, countTokens |
| `gemini-flash-latest` | Gemini Flash Latest | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-flash-lite-latest` | Gemini Flash-Lite Latest | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-pro-latest` | Gemini Pro Latest | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-2.5-flash-lite` | Gemini 2.5 Flash-Lite | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-2.5-flash-image` | Nano Banana | generateContent, countTokens, batchGenerateContent |
| `gemini-3-flash-preview` | Gemini 3 Flash Preview | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.1-pro-preview` | Gemini 3.1 Pro Preview | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.1-pro-preview-customtools` | Gemini 3.1 Pro Preview Custom Tools | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.1-flash-lite-preview` | Gemini 3.1 Flash Lite Preview | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.1-flash-lite` | Gemini 3.1 Flash Lite | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3-pro-image-preview` | Nano Banana Pro | generateContent, countTokens, batchGenerateContent |
| `gemini-3-pro-image` | Nano Banana Pro | generateContent, countTokens, batchGenerateContent |
| `nano-banana-pro-preview` | Nano Banana Pro | generateContent, countTokens, batchGenerateContent |
| `gemini-3.1-flash-image-preview` | Nano Banana 2 | generateContent, countTokens, batchGenerateContent |
| `gemini-3.1-flash-image` | Nano Banana 2 | generateContent, countTokens, batchGenerateContent |
| `gemini-3.1-flash-lite-image` | Nano Banana 2 Lite | generateContent, countTokens, batchGenerateContent |
| `gemini-3.5-flash` | Gemini 3.5 Flash | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.5-flash-lite` | Gemini 3.5 Flash Lite | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-omni-flash-preview` | Gemini Omni Flash Preview | generateContent, countTokens |
| `gemini-omni-1.1-flash` | Gemini Omni 1.1 Flash | generateContent, countTokens |
| `gemini-3.5-transcribe` | Gemini 3.5 Transcribe | generateContent, countTokens |
| `gemini-3.6-flash` | Gemini 3.6 Flash | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.7-flash` | Gemini 3.7 Flash | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-3.8-flash` | Gemini 3.8 Flash | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `lyria-3-clip-preview` | Lyria 3 Clip Preview | generateContent, countTokens |
| `lyria-3-pro-preview` | Lyria 3 Pro Preview | generateContent, countTokens |
| `lyria-3.5` | Lyria 3.5 | generateContent, countTokens |
| `gemini-3.1-flash-tts-preview` | Gemini 3.1 Flash TTS Preview | generateContent, countTokens, batchGenerateContent |
| `gemini-3.8-flash-tts` | Gemini 3.8 Flash TTS | generateContent, countTokens, batchGenerateContent |
| `gemini-3.8-flash-lite-tts` | Gemini 3.8 Flash Lite TTS | generateContent, countTokens, batchGenerateContent |
| `gemini-robotics-er-2-preview` | Gemini Robotics-ER 2 Preview | generateContent, countTokens, createCachedContent, batchGenerateContent |
| `gemini-2.5-computer-use-preview-10-2025` | Gemini 2.5 Computer Use Preview 10-2025 | generateContent, countTokens |
| `antigravity-preview-05-2026` | Antigravity Agent Preview | generateContent, countTokens |
| `antigravity-preview-09-2026` | Antigravity Agent Preview | generateContent, countTokens |
| `antigravity-preview-latest` | Antigravity Agent Preview Latest | generateContent, countTokens |
| `deep-research-max-preview-04-2026` | Deep Research Max Preview (Apr-21-2026) | generateContent, countTokens |
| `deep-research-preview-04-2026` | Deep Research Preview (Apr-21-2026) | generateContent, countTokens |
| `deep-research-pro-preview-12-2025` | Deep Research Pro Preview (Dec-12-2025) | generateContent, countTokens |
| `gemini-embedding-001` | Gemini Embedding 001 | embedContent, countTextTokens, countTokens, asyncBatchEmbedContent |
| `gemini-embedding-2-preview` | Gemini Embedding 2 Preview | embedContent, countTextTokens, countTokens, asyncBatchEmbedContent |
| `gemini-embedding-2` | Gemini Embedding 2 | embedContent, countTextTokens, countTokens, asyncBatchEmbedContent |
| `aqa` | Model that performs Attributed Question Answering. | generateAnswer |
| `veo-3.1-generate-preview` | Veo 3.1 | predictLongRunning |
| `veo-3.1-fast-generate-preview` | Veo 3.1 fast | predictLongRunning |
| `veo-3.1-lite-generate-preview` | Veo 3.1 lite | predictLongRunning |
| `gemini-3.5-transcribe-live` | Gemini 3.5 Transcribe Live | bidiGenerateContent |
| `gemini-2.5-flash-native-audio-latest` | Gemini 2.5 Flash Native Audio Latest | countTokens, bidiGenerateContent |
| `gemini-2.5-flash-native-audio-preview-09-2025` | Gemini 2.5 Flash Native Audio Preview 09-2025 | countTokens, bidiGenerateContent |
| `gemini-2.5-flash-native-audio-preview-12-2025` | Gemini 2.5 Flash Native Audio Preview 12-2025 | countTokens, bidiGenerateContent |
| `gemini-3.1-flash-live-preview` | Gemini 3.1 Flash Live Preview | bidiGenerateContent |
| `gemini-3.8-live` | Gemini 3.8 Live | bidiGenerateContent |
| `gemini-3.8-live-extended-thinking` | Gemini 3.8 Live Extended Thinking | bidiGenerateContent |
| `gemini-robotics-er-2-streaming-preview` | Gemini Robotics-ER 2 Streaming Preview | bidiGenerateContent |
| `gemini-3.5-live-translate-preview` | Gemini 3.5 Live Translate Preview | bidiGenerateContent |
| `lyria-realtime-exp` | Lyria Realtime Experimental | bidiGenerateMusic |
