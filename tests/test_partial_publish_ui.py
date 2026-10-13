import subprocess
from pathlib import Path


def test_partial_render_exposes_publish_for_ready_files_only():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const context = vm.createContext({document:{addEventListener(){}}, console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const job = {id:'localjob', status:'FAILED', error:'INCOMPLETE_RENDER_COUNT',
  renders:[{rank:1, success:true, storage_status:'LOCAL'},
           {rank:2, success:false}]};
assert.equal(vm.runInContext('canPublishRenders', context)(job), true);
assert.equal(vm.runInContext('publishableJobStatus', context)(job), true);
job.renders[0].publish_status = 'UPLOADED';
assert.equal(vm.runInContext('canPublishRenders', context)(job), false);
job.renders[0].publish_status = undefined;
job.error = 'ANALYSIS_FAILED';
assert.equal(vm.runInContext('canPublishRenders', context)(job), false);
job.error = 'INCOMPLETE_RENDER_COUNT';
job.renders[0].success = false;
assert.equal(vm.runInContext('canPublishRenders', context)(job), false);
"""
    app_js = Path(__file__).resolve().parents[1] / "app/ui/app.js"
    subprocess.run(["node", "-e", script, str(app_js)], check=True, capture_output=True)


def test_placeholder_title_disables_publishing_until_recovered():
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const context = vm.createContext({document:{addEventListener(){}}, console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const canPublish = vm.runInContext('canPublishRenders', context);
const job = {status:'COMPLETED', renders:[{rank:1, success:true, storage_status:'LOCAL',
  youtube_metadata:{title:'Source excerpt 06:40–07:00'}}]};
assert.equal(canPublish(job), false);
job.renders[0].youtube_metadata.title = 'He lifts the bar with one hand';
job.renders[0].metadata_status = 'GENERATED';
assert.equal(canPublish(job), true);
job.renders[0].metadata_status = 'REVIEW_REQUIRED';
assert.equal(canPublish(job), false);
"""
    app_js = Path(__file__).resolve().parents[1] / 'app/ui/app.js'
    subprocess.run(['node', '-e', script, str(app_js)], check=True, capture_output=True)
