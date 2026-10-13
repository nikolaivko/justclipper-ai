const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../app/narrated/ui.html'), 'utf8').split('<script>')[1].split('</script>')[0];
const nodes = new Map(), storage = new Map();
const element = id => {
  if (!nodes.has(id)) nodes.set(id, {value:'', hidden:false, disabled:false, textContent:'',
    replaceChildren(){}, add(){}, querySelector(){return element(id+'-button')},
    pause(){}, load(){}, removeAttribute(){}});
  return nodes.get(id);
};
let responseStatus = 404;
const context = vm.createContext({
  document:{getElementById:element}, Option:function(){},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  clearTimeout(){},setTimeout(){},
  fetch:async url => url==='/api/voices' ? {ok:true,json:async()=>[]} :
    {ok:false,status:responseStatus,json:async()=>({detail:'Test error'})}
});
vm.runInContext(script, context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  const seed=()=>{storage.set('narrated.job','a'.repeat(32));vm.runInContext("jobId='a'.repeat(32)",context);element('job').hidden=false;};
  seed();await vm.runInContext('poll()',context);
  assert.equal(storage.has('narrated.job'),false);
  assert.equal(element('job').hidden,true);
  assert.equal(element('planButton').disabled,false);
  seed();await vm.runInContext('closeProject()',context);
  assert.equal(storage.has('narrated.job'),false);
  responseStatus=500;seed();
  await assert.rejects(vm.runInContext('closeProject()',context));
  assert.equal(storage.has('narrated.job'),true);
  console.log('PASS: stale status clears state; repeated close works; server failure preserves project.');
})().catch(e=>{console.error(e);process.exitCode=1;});
