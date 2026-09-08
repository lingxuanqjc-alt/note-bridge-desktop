const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {allowRead,validateScope,selectedGroupListReady,run,runBatch}=require('../scripts/honor-matrix-browser.cjs');

test('OPPO default label requires the backend-minted exact F2 note and group binding',()=>{
 const selected={...validScope(1),group_name:'未分类'};
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

test('only observed Honor reads and official static assets pass the network gate',()=>{
 for(const [method,url,type] of [
  ['GET','https://cloud.honor.com/portal/notepad','document'],
  ['GET','https://cloud.honor.com/portal/user/info','xhr'],
  ['GET','https://cloud.honor.com/portal/config/web','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/noteDetail','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/note/util/getNoteList','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/note/initDataBase/getFolderList','xhr'],
  ['GET','https://cloud.honor.com/portal/note/main.js','script'],
  ['GET','https://download.cloud.hihonor.com/portal/notepad/file/singleFileDownstream?cloudPath=synthetic','image']
 ])assert.equal(allowRead(method,url,type),true,url);
});

test('required usage bootstrap is only an exact official GET, never an auth or write allowance',()=>{
 const path='/portal/user/usage';
 assert.equal(allowRead('GET','https://cloud.honor.com'+path,'xhr'),true,
  'The official useUsage read must finish before getFullPageData can load the group list.');
 for(const [method,url] of [
  ['POST','https://cloud.honor.com'+path],['HEAD','https://cloud.honor.com'+path],
  ['PUT','https://cloud.honor.com'+path],['DELETE','https://cloud.honor.com'+path],
  ['GET','https://other.honor.com'+path],['GET','https://cloud.hihonor.com'+path],
  ['GET','https://cloud.honor.com.evil.invalid'+path],['GET','http://cloud.honor.com'+path],
  ['GET','https://cloud.honor.com:8443'+path],['GET','https://cloud.honor.com'+path+'/other'],
  ['GET','https://cloud.honor.com/portal/authorization/remoteLoginUrl?lang=zh-cn']
 ])assert.equal(allowRead(method,url,'xhr'),false,method+' '+url);
});

test('full-page initialization only permits the official empty-body POST that precedes folder rendering',()=>{
 const path='/portal/notepad/initDataBase/initGetFullPageData',url='https://cloud.honor.com'+path;
 for(const body of [undefined,null,''])assert.equal(allowRead('POST',url,'xhr',body),true);
 for(const method of ['GET','HEAD','PUT','DELETE','PATCH'])assert.equal(allowRead(method,url,'xhr'),false);
 for(const address of ['https://other.honor.com'+path,'https://cloud.hihonor.com'+path,
  'https://cloud.honor.com:8443'+path,'http://cloud.honor.com'+path,'https://cloud.honor.com.evil.invalid'+path,
  url+'?noteIds=synthetic',url+'/other'])assert.equal(allowRead('POST',address,'xhr'),false,address);
 for(const body of ['{}','[]','{"noteIds":["synthetic"]}','{"update":true}']){
  assert.equal(allowRead('POST',url,'xhr',body),false,'The observed initialization supplies no mutation or selection payload.');
 }
 assert.equal(allowRead('GET','https://cloud.honor.com/portal/authorization/remoteLoginUrl?lang=zh-cn','xhr'),false,
  'A separate blocked login URL has no proven dependency on folder initialization.');
});

function validScope(index){
 return {fixtureId:index.toString(16).padStart(32,'0'),title:'笔记互迁验收 · 范围',expectedText:'正文',
  group_name:'笔记互迁分组验收 T',group_id:'b'.repeat(32),expected_images:0,image_specs:[],
  expected_styles:[],headings:[],todos:[],lists:[]};
}

function listDocument(selected,{active=true,group=selected.group_id,title=selected.group_name,folders=[selected.group_id]}={}){
 const item={__reactFiber$synthetic:{memoizedProps:{data:{key:group}}},
  getBoundingClientRect:()=>({width:10,height:10}),classList:{contains:()=>active},
  querySelector:()=>({textContent:title})};
 const rows=folders.map(folder=>({__reactFiber$synthetic:{memoizedProps:{note:{folder_uuid:folder}}},
  getBoundingClientRect:()=>({width:10,height:10})}));
 return {querySelectorAll:selector=>selector==='.cardlist'?rows:[item]};
}

test('selected highlight cannot certify an old, empty, mixed or differently owned note list',()=>{
 const selected=validScope(1),predicate=selectedGroupListReady.toString();
 for(const options of [{active:false},{group:'c'.repeat(32)},{title:'different'},
  {folders:[]},{folders:['c'.repeat(32)]},{folders:[selected.group_id,'c'.repeat(32)]}]){
  assert.equal(vm.runInNewContext('('+predicate+')(selected)',{document:listDocument(selected,options),selected}),false);
 }
 assert.equal(vm.runInNewContext('('+predicate+')(selected)',{document:listDocument(selected),selected}),true);
});

function localBrowserHarness(failIdentity,exerciseStaleList=false){
 const counts={launch:0,context:0,page:0,cookies:0,navigate:0,resetMarkers:0,close:0,route:0,identities:[],readiness:[]};
 let listReady=!exerciseStaleList;
 const page={
  goto:async()=>counts.navigate++,evaluate:async()=>counts.resetMarkers++,waitForTimeout:async()=>{},
  waitForFunction:async(fn,scope)=>{
   if(fn.name==='selectedGroupListReady'&&exerciseStaleList){
    for(const folders of [['c'.repeat(32)],[],[scope.group_id]]){
     listReady=vm.runInNewContext('('+fn.toString()+')(scope)',{document:listDocument(scope,{folders}),scope});
     counts.readiness.push(listReady);
    }
    assert.equal(listReady,true);
   }
   if(fn.toString().includes('provisionalNoteInfo')){
    counts.identities.push(scope.fixtureId);
    if(scope.fixtureId===failIdentity)throw new Error('sensitive server detail never returned');
   }
  },
  locator:selector=>({
   first(){return this;},waitFor:async()=>{},scrollIntoViewIfNeeded:async()=>{},click:async()=>{},
   evaluateAll:async()=>{
    if(selector.includes('noteTreeNodeItem'))return 1;
    assert.equal(listReady,true,'Never interpret a stale one-row list as identity_missing.');
    return {matches:1,rows:4};
   },
   evaluate:async()=>({bodyTextMatches:true,images:[],styles:[],headings:[],todos:[],lists:[]})
  })
 };
 const context={route:async()=>counts.route++,addCookies:async()=>counts.cookies++,newPage:async()=>{counts.page++;return page;}};
 const browser={newContext:async()=>{counts.context++;return context;},close:async()=>counts.close++};
 const sandbox={module:{exports:{}},process:{env:{}},console,require:name=>{
  assert.equal(name,'playwright');return {chromium:{launch:async()=>{counts.launch++;return browser;}}};
 }};
 vm.runInNewContext(fs.readFileSync(require.resolve('../scripts/honor-matrix-browser.cjs'),'utf8'),sandbox);
 return {counts,runBatch:sandbox.module.exports.runBatch};
}

test('four scoped items reuse one browser, context, login and page while resetting stale markers',async()=>{
 const harness=localBrowserHarness(),cookies=[{value:'not-real-private-value'}];
 const result=await harness.runBatch({scopes:[1,2,3,4].map(validScope),cookies});
 assert.equal(result.status,'verified');assert.equal(result.checked,4);assert.equal(result.remaining,0);
 assert.equal(harness.counts.launch,1);assert.equal(harness.counts.context,1);
 assert.equal(harness.counts.page,1);assert.equal(harness.counts.cookies,1);assert.equal(harness.counts.navigate,1);
 assert.equal(harness.counts.resetMarkers,4);assert.equal(harness.counts.close,1);assert.equal(harness.counts.route,1);
 assert.equal(new Set(harness.counts.identities).size,4);assert.equal(cookies.length,0);
 assert.ok(result.items.every((item,index)=>item.index===index&&item.identity_matched));
 assert.ok(!JSON.stringify(result).includes('not-real-private-value'));
});

test('identity selection waits through the prior group list and loading gap before scanning',async()=>{
 const harness=localBrowserHarness(undefined,true);
 const result=await harness.runBatch({scopes:[validScope(2)],cookies:[]});
 assert.equal(result.status,'verified');assert.equal(result.checked,1);
 assert.deepEqual(harness.counts.readiness,[false,false,true]);
 assert.equal(harness.counts.identities.length,1);assert.equal(harness.counts.close,1);
});

test('identity failure stops later native selections and closes the shared authenticated context',async()=>{
 const harness=localBrowserHarness(validScope(2).fixtureId),cookies=[{value:'not-real-private-value'}];
 const result=await harness.runBatch({scopes:[1,2,3,4].map(validScope),cookies});
 assert.equal(result.status,'needs_review');assert.equal(result.checked,2);assert.equal(result.remaining,2);
 assert.equal(result.items[1].status,'blocked');assert.equal(result.items[1].stage,'editor_identity');
 assert.equal(harness.counts.identities.length,2);assert.equal(harness.counts.close,1);assert.equal(cookies.length,0);
 assert.ok(!JSON.stringify(result).includes('sensitive server detail'));
});

test('all scopes validate before any browser opens and duplicate identities cannot create misleading batch counts',async()=>{
 for(const scopes of [[],[validScope(1),validScope(1)],[validScope(1),{...validScope(2),expectedText:''}],
  [{...validScope(1),cookies:[{value:'per-item-auth-forbidden'}]}]]){
  const cookies=[{value:'not-real-private-value'}];
  const result=await runBatch({scopes,cookies});
  assert.equal(result.status,'blocked');assert.equal(result.stage,'scope');assert.equal(result.items.length,0);
  assert.equal(cookies.length,0);assert.ok(!JSON.stringify(result).includes('not-real-private-value'));
 }
});

test('cloud mutations, unobserved POSTs, external hosts and telemetry never pass',()=>{
 for(const [method,url,type] of [
  ['POST','https://cloud.honor.com/portal/notepad/noteSave','xhr'],
  ['GET','https://cloud.honor.com/portal/notepad/noteSave','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/note/folder/update','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/file/upload','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/file/preCreateFile','xhr'],
  ['DELETE','https://cloud.honor.com/portal/authorization/signOut','xhr'],
  ['GET','https://cloud.honor.com/portal/authorization/getSmsCode','xhr'],
  ['POST','https://cloud.honor.com/portal/notepad/unobserved','xhr'],
  ['GET','https://cloud.honor.com/report/pixel.png','image'],
  ['GET','https://cloud.honor.com.evil.invalid/portal/note/main.js','script'],
  ['GET','http://cloud.honor.com/portal/note/main.js','script'],
  ['POST','https://other.honor.com/portal/notepad/noteDetail','xhr'],
  ['GET','https://example.com/note-bridge','document']
 ])assert.equal(allowRead(method,url,type),false,url);
});

test('scope failure returns only a safe code before loading Playwright or opening a browser',async()=>{
 const result=await run({title:'private original title',fixtureId:'not-confirmed',cookies:[{value:'never-output'}]});
 assert.equal(result.status,'blocked');assert.equal(result.code,'scope');
 assert.ok(!JSON.stringify(result).includes('never-output'));
 assert.ok(!JSON.stringify(result).includes('private original title'));
});

test('nonempty exact synthetic ID, group and image shape are mandatory',()=>{
 const scope={fixtureId:'a'.repeat(32),title:'笔记互迁验收 · 范围',expectedText:'正文',
  expected_images:0,image_specs:[],expected_styles:[],headings:[],todos:[],lists:[]};
 assert.doesNotThrow(()=>validateScope(scope));
 assert.doesNotThrow(()=>validateScope({...scope,group_name:'笔记互迁分组验收 M (2) (3)',group_id:'b'.repeat(32)}));
 assert.throws(()=>validateScope({...scope,group_name:'笔记互迁分组验收 M'+ ' (2)'.repeat(9),group_id:'b'.repeat(32)}));
 for(const change of [{fixtureId:'../outside'},{expectedText:'  '},{expected_images:1},
  {group_name:'private group',group_id:'b'.repeat(32)},{group_name:'笔记互迁分组验收 T',group_id:'bad'},
  {image_specs:[{id:'image',width:0,height:1}],expected_images:1}]){
  assert.throws(()=>validateScope({...scope,...change}));
 }
});
