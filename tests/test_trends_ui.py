import subprocess
from pathlib import Path


def test_trend_button_state_and_safe_diagnostics():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const context = vm.createContext({document: {addEventListener() {}}, console});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const state = value => vm.runInContext(`trendDownloadState(${JSON.stringify(value)})`, context);
const publicVideo = {privacy_status:'public', upload_status:'processed'};
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'RESTRICTED', acquisition_status:'AVAILABLE'}).enabled, false);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'AVAILABLE'}).enabled, true);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'LICENSED', acquisition_status:'AVAILABLE'}).enabled, true);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'OWNED', acquisition_status:'AVAILABLE'}).enabled, true);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'PERMITTED', acquisition_status:'AVAILABLE'}).enabled, true);
assert.equal(state({...publicVideo, rights_status:'LICENSED', acquisition_status:'AVAILABLE'}).enabled, false);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'COMPLETED'}).enabled, true);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'ACQUISITION_BLOCKED'}).enabled, false);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'AVAILABLE', technical_downloadability:'BLOCKED'}).enabled, false);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'AVAILABLE', upload_status:'deleted'}).enabled, false);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'ACQUISITION_BLOCKED', acquisition_error_code:'BOT_PROTECTION', technical_downloadability:'BLOCKED'}).enabled, true);
assert.equal(state({...publicVideo, id:'abcdefghijk', rights_status:'UNKNOWN', acquisition_status:'AUTH_REQUIRED', acquisition_error_code:'AUTH_REQUIRED', technical_downloadability:'BLOCKED'}).enabled, false);
assert.equal(vm.runInContext(`safeCode('BOT_PROTECTION')`, context), 'BOT_PROTECTION');
assert.equal(vm.runInContext(`safeCode('secret=<token>')`, context), 'UNKNOWN');
"""
    app_js = Path(__file__).resolve().parents[1] / "app/ui/app.js"
    subprocess.run(["node", "-e", script, str(app_js)], check=True, capture_output=True)


def test_trend_cards_render_restrictions_reasons_and_exact_video_id():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const cards = [];
const grid = {innerHTML:'', appendChild(card) {cards.push(card);}};
const elements = {
  'trends-grid': grid,
  'trends-loading': {style:{}},
  'step-trends': {classList:{add() {}}},
  'trends-credential-warning': {style:{}}
};
const document = {
  addEventListener() {},
  querySelectorAll() {return [];},
  getElementById(id) {return elements[id];},
  createElement() {
    const button = {dataset:{}, addEventListener(_event, callback) {this.click = callback;}};
    return {innerHTML:'', className:'', button,
      querySelector(selector) {return selector === '.download-trend-btn' ? button : null;}};
  }
};
const trends = [
  {id:'abcdefghijk', title:'Restricted', rights_status:'RESTRICTED', rights_reason:'PRIVATE_VIDEO', privacy_status:'private', upload_status:'processed', acquisition_status:'AVAILABLE', viewCount:1},
  {id:'mnopqrstuvw', title:'Unknown', rights_status:'UNKNOWN', rights_evidence:'STANDARD_YOUTUBE_LICENSE', privacy_status:'public', upload_status:'processed', acquisition_status:'AVAILABLE', viewCount:1},
  {id:'lmnopqrstuv', title:'Eligible', rights_status:'LICENSED',
   privacy_status:'public', upload_status:'processed', acquisition_status:'ACQUISITION_BLOCKED', acquisition_error_code:'BOT_PROTECTION', technical_downloadability:'BLOCKED', viewCount:2}
];
const context = vm.createContext({document, console,
  fetch: async () => ({json: async () => ({configured:true, trends})})});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  await vm.runInContext('loadTrends()', context);
  assert.equal(cards.length, 3);
  assert.match(cards[0].innerHTML, /disabled[^>]*>Rights restricted/);
  assert.equal(cards[0].button.click, undefined);
  assert.match(cards[1].innerHTML, /Rights: UNKNOWN/);
  assert.match(cards[1].innerHTML, /Reuse permission has not been verified/);
  assert.match(cards[1].innerHTML, /<button[^>]*>Make Shorts<\/button>/);
  assert.equal(cards[1].button.dataset.videoId, 'mnopqrstuvw');
  assert.match(cards[2].innerHTML, /YouTube is blocking automated downloads/);
  assert.match(cards[2].innerHTML, /<button[^>]*>Retry Make Shorts<\/button>/);
  assert.equal(typeof cards[2].button.click, 'function');
  context.startDownload = (id, title) => {context.recorded = {id, title};};
  cards[1].button.click();
  assert.equal(context.recorded.id, 'mnopqrstuvw');
  assert.equal(context.recorded.title, 'Unknown');
})().catch(error => {console.error(error); process.exit(1);});
"""
    app_js = Path(__file__).resolve().parents[1] / "app/ui/app.js"
    subprocess.run(["node", "-e", script, str(app_js)], check=True, capture_output=True)


def test_ui_count_url_handoff_and_real_pagination_contract():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const cards=[],calls=[];
const elements=new Proxy({}, {get(o,k){return o[k] ||= {value:'',style:{},classList:{add(){},remove(){}},appendChild(c){cards.push(c);}};}});
elements['setting-target-count'].value='5';
const document={addEventListener(){},querySelectorAll(){return [];},getElementById(id){return elements[id];},
 createElement(){const button={dataset:{},addEventListener(){}};return {innerHTML:'',querySelector(){return button;}};}};
let pages=0,acquisitionPoll;
const context=vm.createContext({AbortSignal,window:{}, document,console,sessionStorage:{setItem(){}},setInterval(fn){acquisitionPoll=fn;return 1;},clearInterval(){},
 fetch:async(url,init)=>{calls.push({url,body:init?.body&&JSON.parse(init.body)});
  if(url==='/api/download/youtube')return {ok:true,json:async()=>({dl_id:'download-one'})};
  if(url.startsWith('/api/download/'))return {ok:true,json:async()=>({status:'DOWNLOADING',progress:.35})};
  if(url.startsWith('/api/auto-edit/'))return {ok:true,json:async()=>({})};
  if(url.startsWith('/api/trends')){pages++; return {ok:true,json:async()=>({configured:true,next_page_token:pages===1?'cursor:50':null,
   trends:[{id:pages===1?'abcdefghijk':'lmnopqrstuv',title:'Our creator challenge',channel:'Creator',duration_display:'1:04:38',viewCount:1234,
   rights_status:'UNKNOWN',privacy_status:'public',upload_status:'processed'}]})};}
 }});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 context.pollJobStatus=()=>{};
 vm.runInContext("rememberJobId('existing')",context);
 await context.startAutoEdit();
 assert.equal(calls[0].body.target_clip_count,5);assert.equal(calls[0].body.count_mode,'TARGET');
 await context.importYouTubeUrl('https://youtu.be/abcdefghijk?t=20');
 const request=calls.find(c=>c.url==='/api/download/youtube');
 assert.equal(request.body.target_clip_count,5);assert.equal(request.body.auto_edit,true);
 assert.equal(request.body.url,'https://youtu.be/abcdefghijk?t=20');
 await acquisitionPoll();
 assert.match(elements['url-error'].textContent,/35%/);
 await context.loadTrends();await context.loadTrends(false,true);
 assert.equal(cards.length,2);
 assert.equal(calls.filter(c=>c.url.startsWith('/api/trends'))[1].url,'/api/trends?page_token=cursor%3A50');
 assert.match(cards[0].innerHTML,/1:04:38/);assert.match(cards[0].innerHTML,/1,234 views/);
 assert.equal(context.qualityCountMessage({analysis_settings:{target_clip_count:5},candidates:[{},{},{}]}),'3 of 5 quality clips found');
 for(const value of ['AUTO','1','2','3','5','10']){
   context.window.globalClipCount=value; context.globalClipCount=value; if (typeof globalClipCount !== 'undefined') globalClipCount = value;
   const selected=context.selectedAutoEditCount();assert.equal(selected.target_clip_count,value==='AUTO'?null:Number(value));
 }
})().catch(e=>{console.error(e);process.exit(1);});
"""
    result=subprocess.run(['node','-e',script,str(Path(__file__).resolve().parents[1]/'app/ui/app.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_interrupted_poll_releases_button_and_stops_timer():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let clears=0, options;
const ctx=vm.createContext({AbortSignal,console,window:{},document:{addEventListener(){}},
 sessionStorage:{getItem(){return null}},setInterval(){return 1},clearInterval(){clears++},
 fetch:async(url,init)=>{options=init;return {ok:true,json:async()=>({status:'FAILED',error_code:'INTERRUPTED'})}}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const button={disabled:true},status={style:{}};
ctx.followAcquisition('test',status,button);
setTimeout(()=>{
 assert.equal(button.disabled,false);
 assert.match(status.textContent,/interrupted by a restart/);
 assert(clears>0);
 assert(options.signal);
},10);
"""
    import subprocess
    subprocess.run(["node", "-e", script, "app/ui/app.js"], check=True, capture_output=True, text=True)
