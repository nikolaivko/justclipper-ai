"""Regressions for stalled proxy preparation and stale status polling."""
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import config
from app.services.ffmpeg_service import FFmpegService
from app.services.progress_service import progress_tracker
from app.services.proxy_encoder import encode_proxy_command


def test_encoder_streams_progress_before_exit_and_retains_stderr(tmp_path):
    active, updates = set(), []
    script = "import sys,time; print('diagnostic',file=sys.stderr); print('out_time_us=N/A'); print('out_time_us=500000',flush=True); time.sleep(.1); print('out_time_us=1000000',flush=True)"
    def callback(fraction, message):
        updates.append((fraction, bool(active)))
    log = tmp_path / 'encoder.log'
    assert encode_proxy_command([sys.executable, '-u', '-c', script], 1, callback, active, log) == 0
    assert updates == [(.5, True), (.99, True)]
    assert not active
    assert 'diagnostic' in log.read_text()


def test_stalled_encoder_is_terminated(tmp_path):
    active = set()
    log = tmp_path / 'stalled.log'
    result = encode_proxy_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                                  30, None, active, log, stall_seconds=.05)
    assert result == -1
    assert not active
    assert 'no timestamp progress' in log.read_text()


def test_hardware_failure_retries_software_and_validates_full_duration(tmp_path, monkeypatch):
    import app.services.proxy_encoder as encoder
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(config, 'PROJECT_LOGS', tmp_path)
    dest = tmp_path / 'proxy.mp4'
    attempts, updates = [], []
    def encode(command, duration, callback, active, log):
        attempts.append(command[command.index('-c:v') + 1])
        assert '-nostdin' in command and command[command.index('-progress') + 1] == 'pipe:1'
        if len(attempts) == 1:
            dest.write_bytes(b'partial')
            return 187
        assert not dest.exists()
        dest.write_bytes(b'complete')
        callback(.5, 'Encoding')
        return 0
    monkeypatch.setattr(encoder, 'encode_proxy_command', encode)
    monkeypatch.setattr(FFmpegService, 'get_metadata', lambda p: SimpleNamespace(duration=60))
    assert FFmpegService.create_proxy(tmp_path / 'source.mp4', dest, source_duration=60,
                                     progress_callback=lambda f, m: updates.append(f))
    assert attempts == ['h264_videotoolbox', 'libx264']
    assert .5 in updates and updates[-1] == 1


def test_size_limited_partial_proxy_is_not_success(tmp_path, monkeypatch):
    from app.services.storage_lifecycle import InsufficientStorage
    import app.services.proxy_encoder as encoder
    monkeypatch.setattr(config, 'PROJECT_LOGS', tmp_path)
    dest = tmp_path / 'partial.mp4'
    def encode(*args):
        dest.write_bytes(b'partial')
        return 0
    monkeypatch.setattr(encoder, 'encode_proxy_command', encode)
    monkeypatch.setattr(FFmpegService, 'get_metadata', lambda p: SimpleNamespace(duration=10))
    with pytest.raises(InsufficientStorage):
        FFmpegService.create_proxy(tmp_path / 'source.mp4', dest,
                                   source_duration=60, max_output_bytes=100)
    assert not dest.exists()


def test_live_progress_in_status_and_failed_job_does_not_become_complete(tmp_path, monkeypatch):
    from app.api.routes import get_status
    from app.services.clip_service import ClipService
    from app.services.job_store import job_store
    monkeypatch.setattr(job_store, 'jobs_dir', tmp_path)
    tracker = progress_tracker
    monkeypatch.setattr(tracker, 'active_jobs', {})
    job = ClipService.create_job('proxy_progress_test', 'source.mp4', tmp_path / 'source.mp4')
    job_id = job['id']
    tracker.start_job(job_id, 60, 'ANALYSIS')
    tracker.update_stage(job_id, 'PROXY_GENERATION', 'Preparing video')
    ClipService.update_job_status(job_id, 'GENERATING_PROXY')
    saved = job_store.get(job_id)['progress']['overall_progress']
    tracker.update_progress(job_id, .5, 'Half the frames encoded')
    public = get_status(job_id)
    assert public['progress']['overall_progress'] == pytest.approx(.15)
    assert public['progress']['status_message'] == 'Half the frames encoded'
    assert job_store.get(job_id)['progress']['overall_progress'] == saved
    tracker.fail_job(job_id, 'ANALYSIS_PROXY_FAILED')
    ClipService.update_job_status(job_id, 'FAILED', 'ANALYSIS_PROXY_FAILED')
    failed = get_status(job_id)
    assert failed['error'] == 'ANALYSIS_PROXY_FAILED'
    assert failed['progress']['overall_progress'] == pytest.approx(.15)
    assert failed['progress']['estimated_remaining_seconds'] is None
    ClipService.update_job_status(job_id, 'QUEUED_ANALYSIS')
    assert get_status(job_id)['progress'] is None


def test_proxy_error_is_preserved_by_worker(tmp_path, monkeypatch):
    from app.services.clip_service import ClipService
    from app.services.job_store import job_store
    from app.services.worker import PipelineWorker
    monkeypatch.setattr(job_store, 'jobs_dir', tmp_path)
    monkeypatch.setattr(progress_tracker, 'active_jobs', {})
    job = ClipService.create_job('proxy_worker_test', 'source.mp4', tmp_path / 'source.mp4')
    job['lifecycle_version'] = 0
    worker = PipelineWorker()
    def fail(job):
        progress_tracker.start_job(job['id'], 60, 'ANALYSIS')
        progress_tracker.update_stage(job['id'], 'PROXY_GENERATION', 'Preparing video')
        raise RuntimeError('ANALYSIS_PROXY_FAILED')
    monkeypatch.setattr(worker, '_run_analysis', fail)
    worker._process_analysis(job)
    saved = job_store.get(job['id'])
    assert saved['status'] == 'FAILED'
    assert saved['error'] == 'ANALYSIS_PROXY_FAILED'
    assert saved['progress']['overall_progress'] < 1


@pytest.mark.parametrize('path,status,visible', [
    ('/api/status/job_test', 200, False),
    ('/api/status/job_test', 500, True),
    ('/api/status/missing', 404, True),
    ('/api/analyze/job_test', 200, True),
])
def test_only_successful_status_requests_are_quiet(path, status, visible):
    from app.services.logging_filters import StatusFilter
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                               ('127.0.0.1:65526', 'GET', path, '1.1', status), None)
    assert StatusFilter().filter(record) is visible


def test_ui_uses_live_progress_and_stops_polling_at_terminal_states():
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const elements = new Map(), timers = new Map();
let nextTimer = 1, calls = 0, currentResponse;
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    style: {}, textContent: '', disabled: false, attrs: {},
    setAttribute(k,v) { this.attrs[k]=v; }, appendChild() {}, addEventListener() {}
  });
  return elements.get(id);
}
const context = vm.createContext({
  document: {getElementById: element, addEventListener() {}, createElement: element},
  console: {error() {}},
  setInterval(fn) {const id=nextTimer++; timers.set(id,fn); return id;},
  clearInterval(id) {timers.delete(id);},
  fetch: async () => {calls++; return currentResponse;}
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext("currentJobId='job_test'; displayResults=()=>{resultShown=true}; displayCandidates=()=>{candidatesShown=true};", context);
const response = job => ({ok:true, json:async()=>job});
(async () => {
  currentResponse = response({status:'GENERATING_PROXY', progress: {
    overall_progress:.153, stage_progress:.515, status_message:'Preparing video', estimated_remaining_seconds:120
  }});
  vm.runInContext("pollJobStatus('COMPLETED', displayResults)",context);
  await [...timers.values()][0]();
  assert.equal(element('analysis-pct-label').textContent,'15.3%');
  assert.equal(element('analysis-progress-bar').style.width,'15.3%');
  assert.equal(element('render-progress-bar').style.width,'15.3%');
  assert.equal(element('analysis-msg-label').textContent,'Preparing video');
  assert.equal(element('analysis-eta-label').textContent,'About 2 min remaining');
  assert.equal(element('btn-make-shorts').disabled,true);
  for (const [stage, label] of [
    ['UPLOADING_TO_AI','UPLOADING TO AI'],
    ['WAITING_FOR_AI_FILE','WAITING FOR AI FILE'],
    ['ANALYZING_WITH_AI','ANALYZING WITH AI'],
    ['BUILDING_CANDIDATES','BUILDING CANDIDATES']
  ]) {
    currentResponse = response({status:'WAITING_FOR_GEMINI', progress:{
      stage, overall_progress:.45, stage_progress:0,
      status_message:label, estimated_remaining_seconds:null
    }});
    await [...timers.values()][0]();
    assert.equal(element('analysis-stage-label').textContent,label);
    assert.equal(element('analysis-eta-label').textContent,'Time remaining is not yet known');
  }
  currentResponse = response({status:'FAILED', error:'ANALYSIS_PROXY_FAILED', progress:{overall_progress:.153}});
  await [...timers.values()][0]();
  assert.equal(timers.size,0);
  assert.match(element('analysis-status').textContent,/Could not prepare the video/);
  assert.equal(element('btn-make-shorts').disabled,false);

  // A manual-analysis terminal state must also stop an auto-render poller.
  vm.runInContext("pollJobStatus('COMPLETED', displayResults)",context);
  currentResponse = response({status:'READY_TO_RENDER'});
  await [...timers.values()][0]();
  assert.equal(timers.size,0);
  assert.equal(context.candidatesShown,true);

  vm.runInContext("pollJobStatus('COMPLETED', displayResults)",context);
  currentResponse = response({status:'COMPLETED',progress:{overall_progress:1}});
  await [...timers.values()][0]();
  assert.equal(timers.size,0);
  assert.equal(context.resultShown,true);
  assert.equal(element('analysis-pct-label').textContent,'100%');

  // Repeated HTTP failures must not silently poll forever.
  vm.runInContext("pollJobStatus('COMPLETED', displayResults)",context);
  currentResponse = {ok:false,status:500};
  for(let i=0;i<3;i++) await [...timers.values()][0]();
  assert.equal(timers.size,0);
  assert.match(element('analysis-status').textContent,/Reload the page/);

  // Slow requests cannot overlap; results from an old poller are ignored.
  let release;
  context.fetch = () => {calls++; return new Promise(resolve=>{release=resolve;});};
  vm.runInContext("pollJobStatus('COMPLETED', displayResults)",context);
  const tick=[...timers.values()][0], before=calls;
  const pending=tick();
  await tick();
  assert.equal(calls,before+1);
  vm.runInContext("pollJobStatus('COMPLETED', displayResults)",context);
  release(response({status:'FAILED',error:'ANALYSIS_PROXY_FAILED'}));
  await pending;
  assert.equal(timers.size,1);
})().catch(e=>{console.error(e);process.exit(1);});
"""
    subprocess.run(['node', '-e', script, str(Path(__file__).resolve().parents[1] / 'app/ui/app.js')],
                   check=True, capture_output=True, text=True)


def test_proxy_eta_accounts_for_actual_encoding_speed(monkeypatch):
    tracker = progress_tracker
    monkeypatch.setattr(tracker, 'active_jobs', {})
    monkeypatch.setattr(tracker, 'timings', {})
    clock = [1000.0]
    monkeypatch.setattr('app.services.progress_service.time.time', lambda: clock[0])
    tracker.start_job('eta_test', 1800, 'ANALYSIS')
    tracker.update_stage('eta_test', 'PROXY_GENERATION', 'Preparing video')
    clock[0] += 30
    tracker.update_progress('eta_test', 30 / 1800)
    # 1770 seconds just for the unencoded frames, plus later analysis.
    assert tracker.get_job_progress('eta_test')['estimated_remaining_seconds'] >= 1770
