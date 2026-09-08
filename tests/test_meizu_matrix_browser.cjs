const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {allowRead,validateScope,initialPageState,run,runBatch}=require('../scripts/meizu-matrix-browser.cjs');

test('OPPO default label requires the backend-minted exact F2 note and group binding',()=>{
 const selected={...scope(1),group_name:'未分类'};
 const binding={fixture:'fixture-20260906-oppo-image-F2',source_fingerprint:'d'.repeat(64),
  target_id:selected.fixtureId,target_group_id:selected.group_id};
 assert.throws(()=>validateScope(selected),/group_scope/,'A label alone cannot broaden the allowed synthetic groups.');
 assert.doesNotThrow(()=>validateScope({...selected,default_group_binding:binding}));
 assert.doesNotThrow(()=>validateScope({...selected,group_name:'未分类 (2)',default_group_binding:binding}));
 for(const change of [{fixture:'fixture-20260907-oppo-complex-OR1'},{target_id:'e'.repeat(32)},
  {target_group_id:'f'.repeat(32)},{source_fingerprint:'unverified'}]){
  assert.throws(()=>validateScope({...selected,default_group_binding:{...binding,...change}}),/group_scope/);
 }
 for(const name of ['任意私人分组','未分类 extra','笔记互迁分组验收 I',null,undefined]){
  assert.throws(()=>validateScope({...selected,group_name:name,default_group_binding:binding}),/group_scope/);
 }
 assert.throws(()=>validateScope({...selected,default_group_binding:true}),/group_scope/);
});
function scope(index=1){return {fixtureId:index.toString(16).padStart(32,'0'),title:'笔记互迁验收 · 范围',expectedText:'正文',
 group_name:'笔记互迁分组验收 T',group_id:'b'.repeat(32),expected_images:0,image_specs:[],expected_styles:[],headings:[],todos:[],lists:[]};}
test('official static assets, login info and the three observed note reads are allowed',()=>{
 for(const [method,url,type] of [
  ['GET','https://notes.flyme.cn/','document'],['GET','https://notes.flyme.cn/c/login/getLoginInfo','xhr'],
  ['POST','https://notes.flyme.cn/c/browser/note/gettags','xhr'],['POST','https://notes.flyme.cn/c/browser/note/getnotegroups','xhr'],
  ['POST','https://notes.flyme.cn/c/browser/note/getnotebycontent','xhr'],['GET','https://notes.flyme.cn/assets/index.js','script'],
  ['GET','https://notes.flyme.cn/synthetic.png','image']])assert.equal(allowRead(method,url,type),true,url);
});
test('autosave, group changes, image allocation, deletion, telemetry and unexpected hosts are blocked',()=>{
 for(const endpoint of ['updatenote','addFileToTemp','addNodeTag','moveNoteToNewGroup','batchTop','recyclenote','recovernote','deleteTag','updateNodeTag','unknown']){
  assert.equal(allowRead('POST','https://notes.flyme.cn/c/browser/note/'+endpoint,'xhr'),false,endpoint);
 }
 for(const [url,type] of [['https://notes.flyme.cn.evil.invalid/assets/index.js','script'],['http://notes.flyme.cn/assets/index.js','script'],
  ['https://notes.flyme.cn/report/pixel.png','image'],['https://example.com/note-bridge','document']])assert.equal(allowRead('GET',url,type),false,url);
});
test('invalid or duplicate scopes stop before browser startup and never reveal credentials',async()=>{
 for(const scopes of [[],[scope(),scope()],[scope(),{...scope(2),expectedText:''}],[{...scope(),cookies:[]}]]){
  const cookies=[{value:'fake-private-value'}],result=await runBatch({scopes,cookies});
  assert.equal(result.status,'blocked');assert.equal(result.stage,'scope');assert.equal(result.items.length,0);assert.equal(cookies.length,0);
  assert.ok(!JSON.stringify(result).includes('fake-private-value'));
 }
 const result=await run(null);assert.equal(result.status,'blocked');assert.equal(result.code,'scope');
 assert.throws(()=>validateScope({...scope(),group_id:'-1'}));
 assert.doesNotThrow(()=>validateScope({...scope(),group_name:'笔记互迁分组验收 M ( (2)'}));
 assert.throws(()=>validateScope({...scope(),group_name:'笔记互迁分组验收 M ()'}));
 assert.throws(()=>validateScope({...scope(),group_name:'笔记互迁分组验收 M (2) (3)'}));
 assert.throws(()=>validateScope({...scope(),expected_images:1,image_specs:[{id:'x',width:0,height:1}]}));
});
function localHarness(failId,initialState='ready'){
 const counts={launch:0,context:0,page:0,navigate:0,cookies:0,close:0,resets:0,identities:[]};
 const page={goto:async()=>counts.navigate++,evaluate:async()=>counts.resets++,
  locator:selector=>({first(){return this;},waitFor:async()=>{},click:async()=>{},scrollIntoViewIfNeeded:async()=>{},
   evaluateAll:async()=>1,evaluate:async()=>({bodyTextMatches:true,images:[],styles:[],headings:[],todos:[],lists:[]})}),
  waitForFunction:async(fn,expected)=>{
   if(fn.name==='initialPageState')return{jsonValue:async()=>initialState,dispose:async()=>{}};
   if(fn.toString().includes('selectedNoteUUID')){counts.identities.push(expected.fixtureId);if(expected.fixtureId===failId)throw Error('private-state');}
  }
 };
 const context={route:async()=>{},addCookies:async()=>counts.cookies++,newPage:async()=>{counts.page++;return page;}};
 const browser={newContext:async()=>{counts.context++;return context;},close:async()=>counts.close++};
 const sandbox={module:{exports:{}},process:{env:{}},console,require:name=>{assert.equal(name,'playwright');return{chromium:{launch:async()=>{counts.launch++;return browser;}}};}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../scripts/meizu-matrix-browser.cjs'),'utf8'),sandbox);
 return {counts,runBatch:sandbox.module.exports.runBatch};
}
test('four records reuse one login context and page while retaining separate identity proofs',async()=>{
 const harness=localHarness(),cookies=[{value:'fake-private-value'}],result=await harness.runBatch({scopes:[1,2,3,4].map(scope),cookies});
 assert.equal(result.status,'verified');assert.equal(result.checked,4);assert.equal(result.remaining,0);
 for(const key of ['launch','context','page','navigate','cookies','close'])assert.equal(harness.counts[key],1,key);
 assert.equal(harness.counts.resets,4);assert.equal(new Set(harness.counts.identities).size,4);assert.equal(cookies.length,0);
 assert.ok(result.items.every((item,index)=>item.index===index&&item.identity_matched));
});
test('uncertain editor identity stops the batch and closes the authenticated context',async()=>{
 const harness=localHarness(scope(2).fixtureId),cookies=[],result=await harness.runBatch({scopes:[1,2,3,4].map(scope),cookies});
 assert.equal(result.status,'needs_review');assert.equal(result.checked,2);assert.equal(result.remaining,2);
 assert.equal(result.items[1].stage,'editor_identity');assert.equal(harness.counts.close,1);
 assert.ok(!JSON.stringify(result).includes('private-state'));
});
test('official agreement gate is identified without consenting, reading an account or changing storage',()=>{
 const node=(text,visible=true)=>({textContent:text,getBoundingClientRect:()=>({width:visible?600:0,height:visible?400:0})});
 const inspect=(privacy,sidebar)=>vm.runInNewContext(`(${initialPageState.toString()})()`,{
  document:{querySelector:selector=>selector==='.privacy-text'?privacy:selector==='.sidebar'?sidebar:null}
 });
 const privacy=node('请阅读《笔记隐私政策》和《笔记服务协议》，点击“同意”表示同意上述全部内容');
 assert.equal(inspect(privacy,null),'privacy_confirmation_required');
 assert.equal(inspect(null,node('private titles must never be inspected')),'ready');
 assert.equal(inspect(node(privacy.textContent,false),null),false);
 assert.equal(inspect(node('unrelated text'),null),false);
 assert.equal(inspect(null,null),false);
});
test('new-context privacy gate stops before group or note selection and preserves a precise failure',async()=>{
 const harness=localHarness(undefined,'privacy_confirmation_required'),cookies=[{value:'synthetic-private-value'}];
 const result=await harness.runBatch({scopes:[scope(1),scope(2)],cookies});
 assert.equal(result.status,'needs_review');assert.equal(result.checked,1);assert.equal(result.remaining,1);
 assert.equal(result.items[0].stage,'wait_sidebar');assert.equal(result.items[0].code,'privacy_confirmation_required');
 assert.equal(result.items[0].diagnostics[0].page_state,'privacy_confirmation_required');
 assert.equal(harness.counts.close,1);assert.equal(harness.counts.identities.length,0);assert.equal(cookies.length,0);
 assert.ok(!JSON.stringify(result).includes('synthetic-private-value'));
});
