import subprocess
from pathlib import Path


def test_analysis_button_gives_feedback_and_restores_job_after_reload():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const elements = {};
function element(id) {
  return elements[id] ||= {
    value: '', textContent: '', style: {}, disabled: false,
    focus() {this.focused = true;},
    classList: {add() {}, remove() {}}
  };
}
Object.assign(element('setting-clip-count'), {value: '30'});
Object.assign(element('setting-exact-count'), {checked: true});
Object.assign(element('setting-min-sec'), {value: '10'});
Object.assign(element('setting-max-sec'), {value: '20'});
Object.assign(element('setting-guidance'), {value: ''});
Object.assign(element('setting-aspect'), {value: 'center_crop'});
Object.assign(element('setting-resolution'), {value: '1080x1920'});

const saved = new Map();
const requests = [];
const context = vm.createContext({
  document: {
    addEventListener() {}, getElementById: element, querySelectorAll() {return [];}
  },
  sessionStorage: {
    setItem(key, value) {saved.set(key, value);},
    getItem(key) {return saved.get(key) || null;},
    removeItem(key) {saved.delete(key);}
  },
  fetch: async (url, options) => {
    requests.push({url, options});
    if (url.startsWith('/api/status/')) return {
      ok: true, json: async () => ({id:'uploaded_job', status:'UPLOADED',
        metadata:{filename:'example.mp4', duration:60, width:1920, height:1080,
          file_size:1000}})
    };
    return {ok:true, json:async () => ({status:'Analysis queued'})};
  },
  setInterval() {return 7;}, clearInterval() {}, console
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);

(async () => {
  await vm.runInContext('startAnalysis()', context);
  assert.match(element('analysis-status').textContent, /No active uploaded video/);
  assert.equal(element('analysis-status').style.display, 'block');
  assert.equal(requests.length, 0);

  vm.runInContext("rememberJobId('uploaded_job')", context);
  assert.equal(saved.get('justclipper.activeJobId'), 'uploaded_job');
  await vm.runInContext('startAnalysis()', context);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/analyze/uploaded_job');
  assert.equal(JSON.parse(requests[0].options.body).clip_count, 30);
  assert.equal(JSON.parse(requests[0].options.body).exact_clip_count, true);
  assert.equal(element('btn-analyze').disabled, true);
  assert.equal(element('btn-analyze').textContent, 'Analyzing...');
  assert.equal(element('analysis-progress-container').style.display, 'block');
  assert.match(element('analysis-status').textContent, /Analysis queued/);

  element('btn-analyze').disabled = false;
  element('setting-clip-count').value = '31';
  await vm.runInContext('startAnalysis()', context);
  assert.equal(requests.length, 1);
  assert.match(element('analysis-status').textContent, /between 1 and 30/);
  assert.equal(element('setting-clip-count').focused, true);

  vm.runInContext('forgetJobId()', context);
  assert.equal(saved.has('justclipper.activeJobId'), false);
  saved.set('justclipper.activeJobId', 'uploaded_job');
  await vm.runInContext('restoreCurrentJob()', context);
  assert.equal(vm.runInContext('currentJobId', context), 'uploaded_job');
  assert.equal(element('metadata-display').innerHTML.includes('example.mp4'), true);
  assert.equal(requests[1].url, '/api/status/uploaded_job');
  assert.match(vm.runInContext("analysisFailureMessage('NO_SUITABLE_CLIPS')", context),
    /smaller target.*NO_SUITABLE_CLIPS/);
  element('setting-exact-count').checked = false;
  await vm.runInContext('reopenAnalysisSettings()', context);
  assert.equal(element('setting-exact-count').checked, true);
  assert.equal(element('btn-analyze').disabled, false);
})().catch(error => {console.error(error); process.exit(1);});
"""
    app_js = Path(__file__).resolve().parents[1] / "app/ui/app.js"
    subprocess.run(["node", "-e", script, str(app_js)], check=True, capture_output=True)
