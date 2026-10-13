# Project-local dav1d optimization

## Installed runtime

- macOS arm64 FFmpeg / FFprobe 9.0.2, statically bundled dav1d 1.5.4.
- Executables: `bin/ffmpeg`, `bin/ffprobe`.
- Source: [Martin Riedl's build service](https://ffmpeg.martin-riedl.de/), pinned build `1789931890_9.0.2`.
- ZIP downloads and published SHA256 files: `tools/ffmpeg-dav1d/downloads/`.
- Verified archive/binary hashes and previous binary hashes: `tools/ffmpeg-dav1d/manifest.json`.
- Previous binaries: `tools/ffmpeg-dav1d/previous/`.
- No compilation, package installation, Homebrew changes, or shell-profile changes were performed.
- `otool -L` reports only existing `/usr/lib` and `/System/Library` dependencies. There is no external dav1d dylib requirement. Build-time `/Volumes/...` strings in the version banner are not runtime library dependencies.

## Measured proxy preparation

Same first 12 seconds of the existing 3840×2160 AV1, 25 fps source. Both encoders produced a 480p H.264/AAC MP4. Input threads 4, filter threads 2, x264 threads 2, preset veryfast, CRF 28. Software encoding was used for a consistent comparison.

| Decoder/build | Wall time |
| --- | ---: |
| Previous FFmpeg 7.0, default libaom | 12.555 s |
| Candidate FFmpeg 9.0.2, forced libaom | 3.498 s |
| Candidate FFmpeg 9.0.2, default dav1d | 1.860 s |

The new default was approximately 6.7× faster than the old default in this sample; dav1d was approximately 1.9× faster than libaom within the candidate build. Logs confirm `av1 (libdav1d) -> h264 (libx264)`, 300 frames and 12 seconds of output. This is a short proxy benchmark, not a claim that the full analysis/render pipeline or every source will be 6.7× faster.

Evidence: `tools/ffmpeg-dav1d/benchmarks/results.json` and adjacent logs. New FFmpeg invocations use the replacement binaries; an already-running encoder keeps its original executable until it exits. No server restart was used to apply the binaries.

## Containment

macOS tool selection rejects external paths, symlink escapes and missing local executables instead of falling back to Homebrew/system FFmpeg. Container deployment resolution is unchanged.

`start-local.command` uses `scripts/run-contained.sh` and the existing macOS `sandbox-exec`. The profile `scripts/project-local.sb` denies filesystem writes outside the checkout, with only non-persistent `/dev/null` and `/dev/tty` device I/O exceptions. Descendants inherit the restriction. Read access to existing system libraries/tools remains allowed. If the sandbox cannot be applied, the launcher fails; it does not retry without containment.

Temporary files, pip, Deno, XDG, Hugging Face, Torch, Clang and compiler caches are configured only in the child environment, under the checkout. No system PATH or global environment configuration is changed.

### OpenMP finding and correction

An initial test run created `/tmp/__KMP_REGISTERED_LIB_<pid>` through the pre-existing PyTorch OpenMP library. LLVM's runtime hardcodes this fallback and does not honor TMPDIR there. The file disappeared automatically when the test process exited. This was a temporary containment violation; it is not omitted from the audit.

After adding the macOS write restriction, a deliberate external write was rejected and PyTorch ran without an external marker. OpenMP may emit warning #179 while falling back from its denied registration file. No external shared cache was deleted or modified by us.

Run all future local checks through the contained launcher:

```sh
scripts/run-contained.sh .venv/bin/python -B -m pytest tests -q --basetemp=data/temp/pytest-local
bash scripts/audit-local-containment.sh
```

The launcher puts the project's `bin` first only in its child-process PATH, including for tests that invoke `ffmpeg` by name. It does not change shell profiles or system PATH.

## Verification

- Full suite with installed binaries and the write restriction: **308 passed**.
- Log: `logs/dav1d-contained-tests.log`.
- Runtime paths and linked-library audit: `logs/dav1d-containment-final.json`.
- External metadata snapshots/comparison: `tools/ffmpeg-dav1d/external-before.json` and `external-after.json`.
- The external comparison covers common cache/install/temp/download destinations and leaves shared files untouched. Thirteen directories were unreadable under the available permissions; the report records that limitation. The OS write restriction provides enforcement for subsequent contained checks, rather than relying only on snapshot coverage.
- Source/job cleanup policy was not changed by this optimization.
