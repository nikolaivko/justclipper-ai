"""Standalone localhost development app. Never imports Clipper's app/worker."""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Literal
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .narrated.core import ROOT, JOBS, VOICES, Plan, probe, run, write_json, MAX_SOURCE_BYTES, youtube_video_id

active = {}  # Only this server's child processes; no interaction with Clipper.
MAX_SECONDS = 7200


def item(base, identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise HTTPException(404, 'Not found')
    path = base/identifier
    if not path.is_dir():
        raise HTTPException(404, 'Not found')
    return path


def stop(identifier):
    record = active.pop(identifier, None)
    if record:
        process, started, log = record
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        log.close()


def reap():
    for identifier, (process, started, log) in list(active.items()):
        if process.poll() is None and time.monotonic()-started <= MAX_SECONDS:
            continue
        folder = JOBS/identifier
        timed_out = process.poll() is None
        stop(identifier)
        state = json.loads((folder/'status.json').read_text()) if (folder/'status.json').exists() else {}
        if state.get('stage') not in {'complete', 'review', 'failed'}:
            write_json(folder/'status.json', dict(stage='failed', progress=0,
                message='Processing timed out. Review the script and retry.' if timed_out else 'Worker stopped unexpectedly. See the local job log.'))


async def monitor():
    while True:
        reap()
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    JOBS.mkdir(parents=True, exist_ok=True)
    VOICES.mkdir(parents=True, exist_ok=True)
    # Recover interrupted jobs honestly instead of displaying endless progress.
    for p in JOBS.glob('*/status.json'):
        state = json.loads(p.read_text())
        if state.get('stage') not in {'complete', 'review', 'failed'}:
            write_json(p, dict(stage='failed', progress=0, message='Interrupted by server restart. Retry or close this project.'))
    task = asyncio.create_task(monitor())
    yield
    task.cancel()
    for identifier in list(active):
        stop(identifier)
    # Closing this development app ends its projects. Voice profiles are saved
    # explicitly for reuse and have a separate delete action.
    for p in JOBS.iterdir():
        if p.is_dir() and re.fullmatch(r'[a-f0-9]{32}', p.name):
            shutil.rmtree(p)


app = FastAPI(title='justClipper · Narrated recap preview', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])


@app.middleware('http')
async def local_origin(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} and origin and origin not in {'http://127.0.0.1:8010', 'http://localhost:8010'}:
        return JSONResponse({'detail': 'Use the local narrated-mode page'}, status_code=403)
    return await call_next(request)


def idle():
    reap()
    if active:
        raise HTTPException(409, 'Another narrated task is running. Wait or close that project.')


def launch(identifier, operation):
    idle()
    folder = item(JOBS, identifier)
    write_json(folder/'status.json', dict(stage='starting', progress=1))
    log = (folder/'worker.log').open('a')
    env = dict(os.environ, AUTONOMOUS_TRENDS_ENABLED='false', AUTO_PROCESS_ELIGIBLE_SOURCES='false')
    try:
        process = subprocess.Popen([sys.executable, '-m', 'app.narrated.pipeline', operation, identifier],
            cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        log.close()
        raise
    active[identifier] = process, time.monotonic(), log


async def save_upload(upload, path, max_bytes):
    size = 0
    try:
        with path.open('wb') as out:
            while chunk := await upload.read(1024*1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, 'File is too large')
                if shutil.disk_usage(path.parent).free < len(chunk) + 1024**3:
                    raise HTTPException(507, 'Not enough disk space; keep at least 1 GB free')
                out.write(chunk)
        if not size:
            raise HTTPException(400, 'The file is empty')
    finally:
        await upload.close()


@app.get('/')
def index():
    return FileResponse(ROOT/'app/narrated/ui.html')


@app.get('/api/voices')
def voices():
    return [dict(id=p.parent.name, name=json.loads(p.read_text())['name']) for p in VOICES.glob('*/profile.json')]


@app.post('/api/voices')
async def upload_voice(file: UploadFile = File(...), name: str = Form(...), transcript: str = Form(...), own_voice: bool = Form(...)):
    idle()
    if not own_voice or not 1 <= len(name.strip()) <= 100 or not 10 <= len(transcript.strip()) <= 2000:
        raise HTTPException(400, 'Provide a name, exact transcript, and confirmation that this is your voice')
    identifier = uuid.uuid4().hex
    folder = VOICES/identifier
    folder.mkdir(parents=True)
    try:
        source = folder/'upload.audio'
        await save_upload(file, source, 50*1024**2)
        duration, streams = await asyncio.to_thread(probe, source)
        if not 10 <= duration <= 90 or not any(s['codec_type']=='audio' for s in streams):
            raise HTTPException(400, 'Use 10–90 seconds of clean speech; 30–60 seconds recommended')
        await asyncio.to_thread(run, [ROOT/'bin/ffmpeg', '-nostdin', '-y', '-i', source, '-vn', '-ac', '1', '-ar', '24000', folder/'sample.wav'])
        source.unlink()
        write_json(folder/'profile.json', dict(name=name.strip(), transcript=transcript.strip(), duration=duration))
    except Exception:
        shutil.rmtree(folder)
        raise
    return dict(id=identifier)


@app.delete('/api/voices/{identifier}')
def delete_voice(identifier):
    idle()
    shutil.rmtree(item(VOICES, identifier))
    return {'deleted': True}


@app.post('/api/voices/{identifier}/preview')
def preview_voice(identifier):
    idle()
    item(VOICES, identifier)
    job = uuid.uuid4().hex
    folder = JOBS/job
    folder.mkdir()
    write_json(folder/'request.json', dict(voice_id=identifier))
    launch(job, 'preview')
    return dict(id=job)


@app.post('/api/jobs')
async def new_job(file: UploadFile = File(...), guidance: str = Form('')):
    idle()
    if len(guidance) > 2000:
        raise HTTPException(400, 'Guidance is too long')
    identifier = uuid.uuid4().hex
    folder = JOBS/identifier
    folder.mkdir(parents=True)
    try:
        await save_upload(file, folder/'source.mp4', MAX_SOURCE_BYTES)
        write_json(folder/'request.json', dict(guidance=guidance))
        launch(identifier, 'plan')
    except Exception:
        shutil.rmtree(folder)
        raise
    return dict(id=identifier)


class YoutubeRequest(BaseModel):
    url: str = Field(max_length=2000)
    guidance: str = Field(default='', max_length=2000)


@app.post('/api/jobs/youtube')
def new_youtube_job(request: YoutubeRequest):
    idle()
    try:
        video_id = youtube_video_id(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    identifier = uuid.uuid4().hex
    folder = JOBS/identifier
    folder.mkdir(parents=True)
    try:
        write_json(folder/'request.json', dict(guidance=request.guidance, video_id=video_id))
        launch(identifier, 'youtube')
    except Exception:
        shutil.rmtree(folder)
        raise
    return dict(id=identifier)


@app.get('/api/jobs/{identifier}')
def job_status(identifier):
    reap()
    folder = item(JOBS, identifier)
    state = json.loads((folder/'status.json').read_text())
    if (folder/'plan.json').exists():
        state['plan'] = json.loads((folder/'plan.json').read_text())
    if (folder/'publication.json').exists():
        state['publication'] = json.loads((folder/'publication.json').read_text())
    state['preview'] = (folder/'preview.wav').exists()
    return state


class PublicationOptions(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=5000)
    privacy: Literal['public', 'unlisted', 'private'] = 'public'
    made_for_kids: bool = False


class RenderRequest(BaseModel):
    voice_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    plan: Plan
    auto_publish: bool = False
    publication: PublicationOptions | None = None


@app.post('/api/jobs/{identifier}/render')
def render_job(identifier, request: RenderRequest):
    idle()
    folder = item(JOBS, identifier)
    item(VOICES, request.voice_id)
    if not (folder/'source.mp4').is_file():
        raise HTTPException(409, 'Source was cleaned after completion. Start a new project.')
    try:
        request.plan.check_source(probe(folder/'source.mp4')[0])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    write_json(folder/'plan.json', request.plan.model_dump())
    req = json.loads((folder/'request.json').read_text())
    if request.auto_publish and request.publication is None:
        raise HTTPException(400, 'Provide YouTube title and visibility')
    write_json(folder/'request.json', dict(req, voice_id=request.voice_id,
        auto_publish=request.auto_publish, publication=request.publication.model_dump() if request.publication else None))
    launch(identifier, 'render')
    return {'started': True}


@app.get('/api/jobs/{identifier}/download')
def download(identifier):
    folder = item(JOBS, identifier)
    filename = 'preview.wav' if (folder/'preview.wav').exists() else 'recap.mp4'
    if not (folder/filename).is_file() or json.loads((folder/'status.json').read_text()).get('stage') != 'complete':
        raise HTTPException(409, 'Output is not ready')
    return FileResponse(folder/filename, filename=filename)


@app.delete('/api/jobs/{identifier}')
def close_job(identifier):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise HTTPException(404, 'Not found')
    folder = JOBS/identifier
    stop(identifier)
    if folder.is_dir():
        shutil.rmtree(folder)
    return {'deleted': True}


@app.post('/api/jobs/{identifier}/retry-plan')
def retry_plan(identifier):
    idle()
    folder = item(JOBS, identifier)
    if not (folder/'source.mp4').is_file():
        raise HTTPException(409, 'Source is missing; start a new project')
    launch(identifier, 'plan')
    return {'started': True}


@app.get('/api/youtube/status')
def youtube_status():
    token = ROOT.parents[1]/'data/youtube/token.json'
    try:
        saved = json.loads(token.read_text())
        return {'connected': bool(saved.get('refresh_token') or saved.get('token')),
                'channel': saved.get('channel_title') or saved.get('channel_name') or 'Connected Clipper channel'}
    except (OSError, ValueError):
        return {'connected': False, 'channel': ''}


@app.post('/api/jobs/{identifier}/publish')
def publish_job(identifier, request: PublicationOptions):
    idle()
    folder = item(JOBS, identifier)
    if not (folder/'recap.mp4').is_file():
        raise HTTPException(409, 'Render the recap first')
    result = folder/'publication.json'
    if result.exists() and json.loads(result.read_text()).get('status') in {'uploaded'}:
        raise HTTPException(409, 'Already uploaded or upload result uncertain. Check YouTube Studio.')
    req = json.loads((folder/'request.json').read_text())
    write_json(folder/'request.json', dict(req, publication=request.model_dump()))
    launch(identifier, 'publish')
    return {'started': True}
