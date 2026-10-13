# Local justClipper Installation

justClipper is now a fully self-contained local application. 

## Requirements
- macOS (Apple Silicon optimized, but works on Intel)
- Python 3.11+
- FFmpeg & FFprobe (install via `brew install ffmpeg`)

## Setup
To set up justClipper for the first time, simply double-click or run:
`./setup-local.command`

This will automatically:
1. Create a project-local virtual environment (`.venv`)
2. Install all required dependencies
3. Set up the local directory structure for models, cache, data, and temp files.

## Running justClipper
To start the application:
`./start-local.command`

This script will boot the server, wait for it to become healthy, and open it automatically in your browser at `http://127.0.0.1:8000`.

## Stopping justClipper
To safely stop the application:
`./stop-local.command`

## Deleting justClipper
The justClipper installation is **strictly contained** within this project directory.

Deleting the project root directory removes:
- Application code
- Python environment (`.venv`)
- Downloaded models (`.models`)
- AI and processing caches (`.cache`)
- Managed source media and renders (`data/`)
- Temporary processing files
- Local configuration and YouTube OAuth tokens

To completely uninstall justClipper, just delete this folder.

**External dependencies that will remain:**
- System FFmpeg/FFprobe (if installed via Homebrew)
- Global Python installations (if any)

To verify the containment status at any time, run:
`./uninstall-local.command`
