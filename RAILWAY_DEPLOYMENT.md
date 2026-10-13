# Railway Deployment Guide

This repository has been fully architected and tested for Railway deployment. The application has been abstracted to use `DATA_ROOT` instead of hardcoded paths, decoupled background processing into a Worker loop, and properly isolated state.

## Services
In Railway, you will create a single **Service** from this GitHub repository.
The service will automatically build using the `Dockerfile` in the repository root.

## Volumes
The application requires persistent storage to preserve state across deployments (downloads, models, caches, jobs, outputs).
1. Go to your Service settings in Railway.
2. Add a **Volume** and mount it at `/data`.

## Environment Variables
Set the following variables in Railway:
- `PORT` = `8000` (Railway injects this automatically, but good to know)
- `DATA_ROOT` = `/data`
- `GEMINI_API_KEY` = `your_gemini_key`
- `YOUTUBE_DATA_API_KEY` = `your_youtube_key`
- `AUTONOMOUS_TRENDS_ENABLED` = `true` (if you want autonomous scanning)
- `AUTO_PROCESS_ELIGIBLE_SOURCES` = `true` (if you want it to download/render CC videos automatically)

## Resource Configuration
Ensure the service has sufficient resources. Video processing and YOLO tracking are intensive.
- **CPU**: 4 - 8 vCPUs recommended
- **RAM**: 8GB recommended
- `MAX_HEAVY_JOBS` is set to `1` by default to prevent OOM errors.

## Healthcheck
Railway will use the healthcheck endpoint to determine if your service is up.
- **Path**: `/health`
- The endpoint verifies FFmpeg presence, FFprobe presence, and write access to `/data`.

## Start Command
The `Dockerfile` defaults to `CMD ["python", "app/main.py"]`, which starts the Uvicorn Web Server (on port 8000), the Background Worker, and the Trends Scheduler concurrently in the same container. No changes to the Start Command are required in Railway.

