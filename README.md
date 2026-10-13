# justClipper

justClipper turns long videos into short clips. It uses Gemini to propose moments, lets the user review and adjust the timeline, and renders MP4 files with FFmpeg. Optional YouTube integrations provide trend discovery and publishing through the user's own account.

This repository is a source snapshot prepared for a development portfolio. It includes the current application and regression tests. Credentials, browser cookies, user videos, job records, caches, downloaded models, local executables and the original Git history are excluded.

## Required: add your own Gemini API key

**AI analysis requires your own Gemini API key. No key is included in this repository.** You can create one in [Google AI Studio](https://aistudio.google.com/apikey). Google offers a free tier for eligible models, including the configured `gemini-3.5-flash-lite`, within your project's available quota. Free-tier access is limited; paid usage has separate pricing. See [Google's API key guide](https://ai.google.dev/gemini-api/docs/api-key) and [current pricing](https://ai.google.dev/gemini-api/docs/pricing).

1. Sign in to Google AI Studio and create a key for your own project.
2. Copy `.env.example` to `.env` in the repository root.
3. Open `.env` and replace the blank `GEMINI_API_KEY=` value with your key:

```dotenv
# Placeholder only: replace this with your own private key.
GEMINI_API_KEY=PASTE_YOUR_OWN_KEY_HERE
GEMINI_MODEL=gemini-3.5-flash-lite
```

4. Start or restart justClipper after saving the file. Keep `.env` private; it is excluded by `.gitignore`.

### How many analyses can I run for free each day?

Google does not publish a single daily allowance that applies to every project. Check the selected model's **Requests per day (RPD)** limit in [AI Studio's rate limits](https://aistudio.google.com/usage?tab=rate-limit). Quotas depend on the model, project and usage tier, and can change. Limits apply to the project, so creating another key in the same project does not increase the quota. Daily request quotas reset at midnight Pacific time. [Official rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits).

One video analysis can make several Gemini requests for windows, focused passes, repairs and retries. The number of resulting clips is not the number of API requests. Estimate completed analyses as **daily request quota divided by average requests per analysis**, while also accounting for minute and token limits.

For illustration only: **if your project has 20 requests/day** and an analysis uses **2–5 requests**, that gives roughly **4–10 video analyses/day** before other limits. These inputs are hypothetical, not a verified quota or a measured justClipper average. Longer videos, additional passes and retries can reduce the count. Local FFmpeg rendering does not itself use the Gemini API quota.

## Features

- Video upload, analysis settings and editable clip candidates.
- Target clip count, duration limits, vertical framing and subtitles.
- Gemini analysis with response validation and recovery.
- FFmpeg rendering, output previews and a clip library.
- YouTube trend discovery, rights classification and optional OAuth publishing.
- One internal heavy worker, storage checks and media cleanup.
- A separate Stories mode for English narrated recaps with local voice cloning.

See [worker.py](app/services/worker.py) for processing, [storage_lifecycle.py](app/services/storage_lifecycle.py) for storage, and [source_rights_service.py](app/services/source_rights_service.py) for rights policy.

## YouTube credentials are optional

You can upload your own video, analyze it with Gemini, render clips and download the MP4 files **without configuring YouTube credentials**.

| What you want to do | Credentials needed |
| --- | --- |
| Analyze an uploaded video and download clips | Your Gemini API key; no YouTube credentials |
| Use US Trends discovery | Your `YOUTUBE_DATA_API_KEY` |
| Publish directly to your YouTube channel | Your Google OAuth client ID, client secret and redirect URI, then channel authorization in the app |

A YouTube Data API key alone does not authorize uploading to a channel. For publishing, configure `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET` and `YOUTUBE_OAUTH_REDIRECT_URI` in your private `.env`, register the redirect URI in your Google Cloud project, and connect your channel. YouTube API quotas are separate from Gemini quotas.

## Stories and voice cloning

Stories is a separate local development mode that turns a source video into an **English narrated recap**. Gemini writes a scene-by-scene script tied to source footage. You can edit the narration and review timestamps before rendering a vertical video with captions, your cloned voice and selected original dialogue.

To create a voice profile, upload approximately **30–60 seconds of clean speech** and enter its **exact transcript**. The UI requires confirmation that the recording is your own voice. You can preview the voice before rendering. English reference speech is recommended for the current English narration pipeline.

Speech generation runs locally through a Voicebox adapter using **Qwen3-TTS 0.6B and MLX**. It needs an Apple Silicon Mac, a separate Python environment, the Voicebox source dependency and downloaded model weights. The first preview/render downloads models and needs additional disk space. The voice adapter itself does not require a paid cloud voice API key; Gemini script generation still uses your own Gemini key and quota. Voice cloning quality has not been validated from this publication snapshot.

### Set up and launch Stories

Stories source is included under `tools/narrated-dev/`. It has its own environment, configuration and media directories. Install the main Clipper environment first, then from the repository root:

```sh
cd tools/narrated-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-narrated.lock.txt
cp .env.example .env
# Edit this separate .env and add your own GEMINI_API_KEY.
mkdir -p bin tools
cp ../../bin/ffmpeg bin/ffmpeg
cp ../../bin/ffprobe bin/ffprobe
git clone https://github.com/jamiepine/voicebox.git tools/voicebox
git -C tools/voicebox checkout 51f49dea198384b4eb6087b72c17057c6eb1c1cd
./scripts/run-narrated.sh
```

Open [localhost:8010](http://localhost:8010). After setup, `./start-narrated.command` in the repository root launches this separate mode. The reference dependency revision comes from the existing project integration; new environments may require dependency adjustments. Stories is not included in the main Docker service.

Saved voice profiles stay on your computer until you delete them. Source and intermediate media are removed after successful rendering. **Download the final recap before closing the project or stopping the Stories server**, because the development mode deletes its job files at shutdown. No personal voice recordings, profiles or model weights are included in this repository.

## Setup

The application needs Python and FFmpeg. Configure your Gemini key using the instructions above. YouTube discovery and publishing require separate credentials and configuration.

Start with an isolated environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Add your own key to `.env`. Keep `.env` private. The example also lists optional YouTube OAuth settings. Register your own OAuth redirect URL with Google before connecting a channel.

On macOS, the application expects executable copies of `ffmpeg` and `ffprobe` inside the checkout's `bin/` directory. These binaries are excluded from the repository. If FFmpeg is installed through Homebrew, place copies in `bin/`:

```sh
mkdir -p bin
cp "$(command -v ffmpeg)" bin/ffmpeg
cp "$(command -v ffprobe)" bin/ffprobe
```

The copied executables may still depend on libraries installed by Homebrew. This is a source distribution, not a standalone macOS application bundle.

Run from the repository root:

```sh
python -m app.main
```

Open [localhost:8000](http://localhost:8000). Runtime folders are created inside the checkout by default. Model downloads and video processing need additional disk space.

## Docker

The Dockerfile installs Linux dependencies and uses `python -m app.main` as its startup command:

```sh
docker compose up --build
```

Configure your own environment and storage before using this outside a local development machine. The current code contains local tool paths for the optional PO-token provider; review that setup for your deployment. The Docker configuration has not been deployed from this snapshot.

## Tests

```sh
python -m pytest tests
```

Some tests require Node.js or FFmpeg. Live API experiments in `scripts/` are optional and require your own credentials; they are not part of the offline test suite.

## Rights and limitations

Use videos you own or have permission to process and publish. Public visibility alone does not establish reuse permission. The application keeps rights policy separate from technical acquisition state.

AI suggestions require review. Model availability, quotas and processing limits depend on your environment. The publishing feature uploads to the connected account and can make videos public; review the settings before using it.

## Publishing this snapshot

Create a new repository from this folder. Do not import the production repository's history or copy its `.env`, `data/`, cookies or credentials into this snapshot. See [PUBLICATION_NOTES.md](PUBLICATION_NOTES.md) for the preparation checks and excluded files.

No new license for the application source has been selected in this snapshot. Third-party dependencies and bundled assets retain their respective terms.
