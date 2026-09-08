"""Host initialization must not be mistaken for an expired session or a missing note."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_account_response_only_projects_known_rejection_and_safe_bootstrap_metadata():
    script = r"""
const assert=require('node:assert/strict');
const {accountResponse,bootstrapClass}=require('./scripts/oppo-matrix-browser.cjs');
const data={ssoId:'synthetic-secret',cookie:'synthetic-secret'};
const good=accountResponse(200,{code:0,data},true);
assert.equal(good.codeZero,true);assert.equal(good.ssoIdPresent,true);assert.equal(good.authRejected,false);
assert(!JSON.stringify(good).includes('synthetic-secret'));
for(const status of [401,403])assert.equal(accountResponse(status,undefined).authRejected,true);
for(const code of [1003,1004])assert.equal(accountResponse(200,{code,data},true).authRejected,true);
for(const code of [999,5000,'synthetic-secret','1003'])
 assert.equal(accountResponse(200,{code,data},true).authRejected,false,'Unknown protocol values cannot imply expiration.');
assert.equal(accountResponse(200,undefined).jsonParsed,null);
assert.equal(accountResponse(200,undefined,false).jsonParsed,false);
assert.equal(accountResponse(500,undefined,false).authRejected,false);
for(const path of ['/note/','/note/index.html','/note/navigator.html'])
 assert.equal(bootstrapClass('https://owork-api-cn.oppo.com'+path),'entry');
assert.equal(bootstrapClass('https://owork-api-cn.oppo.com/note/static/build/main.js'),'asset');
for(const url of ['https://evil.invalid/note/index.html',
 'https://owork-api-cn.oppo.com/note/index.html?secret=private',
 'https://owork-api-cn.oppo.com/note/static/private.json'])assert.equal(bootstrapClass(url),null);
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_delayed_microapp_gets_bounded_time_but_account_rejection_stops_before_selection():
    script = r"""
const assert=require('node:assert/strict'),Module=require('node:module');
const {runBatch}=require('./scripts/oppo-matrix-browser.cjs');
const scope={scopeVersion:1,scopeLabel:'wps-oppo-BI1:0',fixtureId:'selected-id',group_id:'selected-group',
 group_name:'synthetic',title:'笔记互迁验收 · synthetic',expectedText:'synthetic body',expectedRuns:[],
 headings:[],todos:[],lists:[],quotes:[],codes:[],tables:[],dividers:0,
 image_specs:[{id:'a',cloud_id:'cloud-a',width:640,height:240},{id:'b',cloud_id:'cloud-b',width:640,height:240}]};
async function scenario(status,code,delay,boot){
 let waits=0,clicked=false,closed=false;const handlers={};
 const page={on:(name,fn)=>{handlers[name]=fn},goto:async()=>{
  const respond=(url,status,json)=>{
   const req={method:()=> 'GET',url:()=>url};handlers.request(req);
   handlers.response({url:()=>url,request:()=>req,status:()=>status,json:async()=>json});
  };
  respond('https://owork-api-cn.oppo.com/owork-server/web/account/v1/userInfo',status,
   {code,data:{ssoId:'synthetic-secret',token:'synthetic-secret'}});
  if(boot){respond('https://owork-api-cn.oppo.com/note/index.html',200,{});
   respond('https://owork-api-cn.oppo.com/note/static/build/main.js',200,{});}
 },evaluate:async fn=>fn.name==='locateRow'?{matches:waits>=delay?1:0}:
  fn.name==='pageDiagnostics'?{listContainers:boot?1:0,appContainerPresent:true,qiankunStarted:boot}:
  {checks:{identity:true},images:[]},waitForTimeout:async ms=>{assert.equal(ms,250);waits++},
  locator:()=>({click:async()=>{clicked=true}})};
 const context={route:async()=>{},addCookies:async()=>{},newPage:async()=>page};
 const browser={newContext:async()=>context,close:async()=>{closed=true}};
 const original=Module._load;Module._load=function(name,...args){return name==='playwright'?
  {chromium:{launch:async()=>browser}}:original.call(this,name,...args)};
 const request={scopes:[scope],cookies:[]};let output;
 try{output=await runBatch(request)}finally{Module._load=original}
 assert(closed);assert.deepEqual(request.cookies,[]);assert(!JSON.stringify(output).includes('synthetic-secret'));
 return {output,waits,clicked};
}
(async()=>{
 const delayed=await scenario(200,0,100,true);
 assert.equal(delayed.output.status,'verified');assert.equal(delayed.waits,100);assert(delayed.clicked);
 assert.deepEqual(delayed.output.diagnostics.bootstrap,{entryRequested:1,entrySucceeded:1,assetRequested:1,assetSucceeded:1});
 for(const [status,code] of [[401,0],[403,0],[200,1003],[200,1004]]){
  const rejected=await scenario(status,code,100,true);
  assert.equal(rejected.output.code,'account_rejected');assert(!rejected.clicked);assert(rejected.waits<2);
 }
 const unknown=await scenario(200,777,1000,false);
 assert.equal(unknown.output.code,'initialization_timeout');assert.equal(unknown.waits,180);assert(!unknown.clicked);
 assert.equal(unknown.output.diagnostics.accountResponses[0].authRejected,false);
 const missing=await scenario(200,0,1000,true);
 assert.equal(missing.output.code,'identity_missing');assert.equal(missing.waits,180);assert(!missing.clicked);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
