/* Exact-route, read-only Huawei target rendering. Cookies and note text remain in RAM. */
function validScope(s) {
 return s?.scopeVersion===1 && /^(wps-huawei-BD(?:18|20):0|honor-huawei-BD19:[01]|xiaomi-huawei-BC3:[012]|meizu-huawei-AA2:[012]|meizu-huawei-AA5:0|oppo-huawei-BI4:[01])$/.test(s.scopeLabel||'')
  && /^[A-Za-z0-9_$-]{1,150}$/.test(s.fixtureId||'') && /^[A-Za-z0-9_$-]{1,150}$/.test(s.group_id||'')
  && typeof s.title==='string' && s.title.startsWith('笔记互迁验收 · ') && typeof s.group_name==='string'
  && typeof s.expectedText==='string' && s.expectedText.length>0 && s.expectedText.length<=100000
  && Array.isArray(s.expectedRuns) && s.expectedRuns.length<=2000
  && s.expectedRuns.every(r=>typeof r.text==='string')
  && s.image_specs?.length===(s.scopeLabel==='meizu-huawei-AA5:0'?0:2)
  && s.image_specs.every(i=>i.width>0&&i.height>0);
}
function allowRequest(method, raw, resourceType, payload, ids) {
 let url;try{url=new URL(raw)}catch{return false}
 const host=url.hostname,path=url.pathname;
 if(url.protocol!=='https:')return false;
 if(/(?:create|update|delete|remove|upload|commit|logout|save|insert|setSticky)/i.test(path))return false;
 if(method==='POST' && host==='cloud.huawei.com') {
  if(path==='/notepad/note/query')return !!payload && ids.has(payload.guid);
  if(['/notepad/notetag/query','/notepad/simplenote/query','/notepad/sync',
      '/html/getCommonParam','/html/getHomeData','/html/getAboutGet','/html/queryCookieValuesByNames'].includes(path))return true;
  if(path==='/proxyserver/driveFileProxy/preProcess')return payload?.httpMethod==='GET'
   && payload.generateSignFlag===false && payload.needToSignUrl==='';
  return false;
 }
 if(!['GET','HEAD'].includes(method))return false;
 if(host==='cloud.huawei.com' && path.startsWith('/proxy/v1/download/')) {
  let inner;try{inner=decodeURIComponent(path.slice('/proxy/v1/download/'.length))}catch{return false}
  const match=inner.match(/^\/v2\/dataSync\/callback\/v1\/1001\/kind\/note\/record\/([^/]+)\/assets\/[^/]+\/revisions\/[^/]+$/);
  return !!match && ids.has(match[1]);
 }
 if(host==='cloud.huawei.com' && resourceType==='document')return path==='/home'||path==='/';
 const official=host==='cloud.huawei.com'||host.endsWith('.hicloud.com')||host.endsWith('.huawei.com');
 return official && ['script','stylesheet','font','image'].includes(resourceType);
}
// Diagnostic names only, NOT a network allowlist. These fixed calls occur in the saved
// official app.2344e79c213164a93642.17.0.0.300.js and the audited memo/file modules.
const DIAGNOSTIC_API_PATHS=new Set([
 '/html/getCommonParam','/html/getHomeData','/html/getPWAHomeData','/html/getAboutGet',
 '/html/queryCookieValuesByNames','/html/setCookieValue','/language/changeLanguage',
 '/refreshLoginStatus','/heartbeatCheck','/nsp/getUserSpace','/CAS/getJsSdkInfo',
 '/notify','/basic/changeUserStatus','/basic/cutoverStat','/basic/postOperationRecord',
 '/basic/spaceClearingTime','/portalLogout','/pcclient/queryPullupStatus',
 '/notepad/note/query','/notepad/notetag/query','/notepad/simplenote/query','/notepad/sync',
 '/proxyserver/driveFileProxy/preProcess'
]);
function classifyBlockedRequest(method,raw,resourceType) {
 const safeMethod=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'].includes(method)?method:'other';
 const safeType=['document','script','stylesheet','image','font','xhr','fetch','media',
  'websocket','eventsource','manifest','texttrack'].includes(resourceType)?resourceType:'other';
 let url;try{url=new URL(raw)}catch{}
 const host=url?.hostname||'';
 const officialHost=host==='cloud.huawei.com'||host.endsWith('.huawei.com')||host.endsWith('.hicloud.com');
 let pathClass='other_path';
 if(host==='cloud.huawei.com'&&DIAGNOSTIC_API_PATHS.has(url.pathname))pathClass=url.pathname;
 else if(officialHost&&['script','stylesheet','image','font'].includes(safeType))pathClass='static_asset';
 else if(safeType==='document')pathClass='document';
 return {method:safeMethod,resourceType:safeType,officialHost,pathClass};
}
// Official Vue 3 runtime keeps the root VNode; the note-detail component owns noteDetail.guid.
// No devtools-only __vueParentComponent or guessed title-based fallback is used.
function inspectEditor({scope,expand=false}) {
 const checks={route:false,detail:false,group:false,title:false,body:false,rendered_body:false,styles:false};
 const route=window.router?.currentRoute?.value;
 checks.route=route?.name==='note' && String(route.params?.itemId)===scope.fixtureId
  && String(route.params?.categoryId)===scope.group_id && route.params?.kind==='note';
 const seen=new Set(),details=[];
 function walk(v){
  if(!v||typeof v!=='object'||seen.has(v)||seen.size>5000)return;seen.add(v);
  if(Array.isArray(v)){v.forEach(walk);return}
  const c=v.component;
  if(c?.type?.name==='note-detail' && c.proxy?.$el?.isConnected
     && c.proxy.$el.contains(document.querySelector('#note_detail_editor')))details.push(c.proxy);
  if(c)walk(c.subTree);walk(v.children);if(v.suspense)walk(v.suspense.activeBranch);
 }
 walk(document.querySelector('#app')?._vnode);
 // Only Home/note are confirmed route names; unrecognized redirects stay other.
 const routeKind=route?.name==='note'?'note':route?.name==='Home'?'home':'other';
 const diagnostics={router:!!route,route_kind:routeKind,
  rootVNode:!!document.querySelector('#app')?._vnode,detailCount:details.length};
 if(details.length!==1)return {checks,diagnostics};
 const detail=details[0],editor=detail.$refs?.editor,cm=editor?.Editor;
 checks.detail=String(detail.noteDetail?.guid)===scope.fixtureId;
 checks.group=String(detail.noteContent?.tag_id)===scope.group_id;
 checks.title=detail.noteTitle===scope.title;
 let expectedText=scope.expectedText,expectedRuns=scope.expectedRuns;
 const projection=scope.editedTitleProjection,metadata=detail.noteContent?.data5;
 diagnostics.edited_title_projection=false;
 if(projection?.firstBlockKind==='paragraph'&&projection.firstBlockText===scope.title
   &&scope.expectedText===scope.title+'\n'+projection.expectedText
   &&Array.isArray(projection.titleRuns)&&Array.isArray(projection.expectedRuns)
   &&JSON.stringify([...projection.titleRuns,...projection.expectedRuns])===JSON.stringify(scope.expectedRuns)
   &&metadata?.data2==='edit'&&metadata.data1===scope.title&&checks.title){
  const titleEditor=document.querySelector('#note_title_editor .CodeMirror')?.CodeMirror;
  checks.title=typeof titleEditor?.getValue==='function'&&titleEditor.getValue()===scope.title;
  if(checks.title){expectedText=projection.expectedText;expectedRuns=projection.expectedRuns;
   diagnostics.edited_title_projection=true}
 }
 diagnostics.editor=!!cm;diagnostics.valueReader=typeof editor?.getValue==='function';
 if(!checks.route||!checks.detail||!checks.group||!cm||!diagnostics.valueReader)return {checks,diagnostics};
 if(expand){cm.setOption('viewportMargin',Infinity);cm.refresh();return {checks,diagnostics,expanded:true}}
 const compact=value=>String(value||'').replace(/\s/g,'');
 const value=editor.getValue();
 if(!Array.isArray(value.simpleLines))return {checks,diagnostics};
 checks.body=compact(value.simpleLines.filter(l=>['Text','Bullet'].includes(l.lineType)).map(l=>l.content||'').join('\n'))===compact(expectedText);
 // CodeMirror stops iteration when its callback returns a truthy value.
 const lines=[];cm.doc.eachLine(l=>{lines.push(l)});
 const pre=[...document.querySelectorAll('#note_detail_editor .CodeMirror-code pre.CodeMirror-line')];
 diagnostics.lines=lines.length;diagnostics.renderedLines=pre.length;
 if(lines.length!==pre.length)return {checks,diagnostics};
 const shown=[];
 for(let i=0;i<lines.length;i++){
  if(lines[i].lineType && !['Text','Bullet'].includes(lines[i].lineType))continue;
  if(!compact(lines[i].text))continue;
  const walker=document.createTreeWalker(pre[i],NodeFilter.SHOW_TEXT);let node;
  while((node=walker.nextNode())){
   const flags={bold:false,italic:false,underline:false,strike:false,highlight:false,code:false,link:null};
   for(let el=node.parentElement;el;el=el.parentElement){
    const css=getComputedStyle(el);
    flags.bold ||= parseInt(css.fontWeight,10)>=600;flags.italic ||= css.fontStyle==='italic';
    flags.underline ||= css.textDecorationLine.includes('underline');flags.strike ||= css.textDecorationLine.includes('line-through');
    flags.highlight ||= el!==pre[i]&&!['transparent','rgba(0, 0, 0, 0)','rgb(255, 255, 255)'].includes(css.backgroundColor);
    flags.code ||= /monospace/i.test(css.fontFamily);if(el.tagName==='A')flags.link=el.getAttribute('href');
    if(el===pre[i])break;
   }
   for(const ch of node.textContent)if(!/\s/.test(ch))shown.push({ch,flags});
  }
 }
 const expected=[];
 for(const run of expectedRuns)for(const ch of run.text)if(!/\s/.test(ch))expected.push({ch,run});
 checks.rendered_body=shown.map(v=>v.ch).join('')===compact(expectedText);
 checks.styles=checks.rendered_body && expected.length===shown.length && expected.every((v,i)=>v.ch===shown[i].ch
  && ['bold','italic','underline','strike','highlight','code'].every(k=>!v.run[k]||shown[i].flags[k])
  && (!v.run.link||v.run.link===shown[i].flags.link));
 return {checks,diagnostics};
}
// Vue's callback guards can leave navigation pending after a failed initialization.
// Initiate navigation without returning its Promise to Playwright; identity proves arrival.
function requestRoute(scope) {
 void window.router.replace({name:'note',params:{categoryId:scope.group_id,kind:'note',itemId:scope.fixtureId}}).catch(()=>{});
}
async function bounded(work, milliseconds, onTimeout=()=>{}) {
 let timer;
 const expired=new Promise((_,reject)=>{timer=setTimeout(()=>{
  try{onTimeout()}catch{}
  const error=new Error('bounded_timeout');error.name='TimeoutError';reject(error);
 },milliseconds)});
 try{return await Promise.race([Promise.resolve().then(work),expired])}
 finally{clearTimeout(timer)}
}
async function prepareRouter(page,timeoutMs=20000) {
 // The official beforeEach completes init/language and addRoute before next().
 // window.router alone exists earlier and does not prove the Home navigation finished.
 await page.waitForFunction(()=>typeof window.router?.isReady==='function'
  &&typeof window.router?.hasRoute==='function',null,{timeout:timeoutMs});
 await bounded(()=>page.evaluate(()=>window.router.isReady().then(()=>true)),timeoutMs);
 await page.waitForFunction(()=>window.router.hasRoute('note'),null,{timeout:timeoutMs});
}
async function runBatch({scopes,cookies},runtime={}) {
 if(!Array.isArray(scopes)||!scopes.length||scopes.length>4||!scopes.every(validScope)
   ||new Set(scopes.map(s=>s.fixtureId)).size!==scopes.length)throw Error('scope');
 const chromium=runtime.chromium||require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright').chromium;
 let browser,closePromise,stage='starting',blocked=0,timedOut=false,cleanupCompleted=true;const items=[];
 const blockedClasses=new Map();let omittedBlockedClasses=0;
 const close=()=>{
  if(browser&&!closePromise){closePromise=Promise.resolve().then(()=>browser.close());closePromise.catch(()=>{})}
  return closePromise;
 };
 try {
  await bounded(async()=>{
  browser=await chromium.launch({headless:true,timeout:15000,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN',serviceWorkers:'block'});
  const ids=new Set(scopes.map(s=>s.fixtureId));
  await context.route('**/*',route=>{
   const req=route.request();let payload;try{payload=req.postDataJSON()}catch{}
   if(allowRequest(req.method(),req.url(),req.resourceType(),payload,ids))return route.continue();
   blocked++;
   const diagnostic=classifyBlockedRequest(req.method(),req.url(),req.resourceType());
   const key=JSON.stringify(diagnostic);
   if(blockedClasses.has(key))blockedClasses.get(key).count++;
   else if(blockedClasses.size<64)blockedClasses.set(key,{...diagnostic,count:1});
   else omittedBlockedClasses++;
   return route.abort();
  });
  await context.addCookies(cookies);cookies.length=0;
  const page=await context.newPage();stage='navigate';
  await page.goto('https://cloud.huawei.com/home',{waitUntil:'domcontentloaded',timeout:30000});
  stage='initial_router_ready';await prepareRouter(page);
  for(const scope of scopes){
   let observed={};stage='exact_route';
   await bounded(()=>page.evaluate(requestRoute,scope),5000);
   const deadline=Date.now()+25000;let expanded=false;
   while(Date.now()<deadline){
    observed=await bounded(()=>page.evaluate(inspectEditor,{scope,expand:!expanded}),5000);
    expanded ||= observed.expanded===true;
    if(Object.values(observed.checks).every(Boolean))break;
    await page.waitForTimeout(250);
   }
   stage='decode_images';
   const images=page.locator('#note_detail_editor .editor_image img');
   if(Object.values(observed.checks).every(Boolean)&&scope.image_specs.length){
    for(let i=0;i<await images.count();i++)await images.nth(i).scrollIntoViewIfNeeded();
    await page.waitForFunction(()=>{
     const nodes=[...document.querySelectorAll('#note_detail_editor .editor_image img')];
     return nodes.length===2&&nodes.every(n=>n.complete&&n.naturalWidth>0&&n.naturalHeight>0);
    },null,{timeout:20000}).catch(()=>{});
   }
   const sizes=await images.evaluateAll(nodes=>nodes.map(n=>({width:n.naturalWidth,height:n.naturalHeight})));
   const imageMatch=sizes.length===scope.image_specs.length&&sizes.every((s,i)=>s.width>0&&s.height>0
    &&Math.abs(s.width/s.height-scope.image_specs[i].width/scope.image_specs[i].height)<0.03);
   // Recheck the identity after asynchronous image loads, before any success result.
   const final=await bounded(()=>page.evaluate(inspectEditor,{scope}),5000);
   const checks={...final.checks,images:imageMatch};
   items.push({scope:scope.scopeLabel,status:Object.values(checks).every(Boolean)?'verified':'needs_review',checks,
    diagnostics:final.diagnostics,images:sizes.length,decoded:sizes.filter(s=>s.width>0&&s.height>0).length,
    source_degradation_count:scope.source_degradation_count});
   if(items.at(-1).status!=='verified')break;
  }
  },runtime.timeoutMs??(60000+50000*scopes.length),()=>{timedOut=true;void close()});
 }catch(error){items.push({status:'blocked',stage,code:['TimeoutError','Error'].includes(error.name)?error.name:'browser_error'})}
 finally{
  if(cookies)cookies.length=0;
  if(browser)try{await bounded(close,5000)}catch{cleanupCompleted=false}
 }
 return {kind:'huawei-exact-target-native',formal_acceptance:false,cloud_writes:0,
  status:cleanupCompleted&&items.length===scopes.length&&items.every(i=>i.status==='verified')?'verified':'needs_review',
  total:scopes.length,checked:items.filter(i=>i.status==='verified').length,items,blocked_requests:blocked,
  blocked_request_classes:[...blockedClasses.values()],unclassified_blocked_count:omittedBlockedClasses,
  timed_out:timedOut||items.some(i=>i.code==='TimeoutError'),cleanup_completed:cleanupCompleted,
  comparison_scope:'exact route and loaded editor identity; full non-whitespace target text; rendered positive styles and decoded images',
  limitations:['Whitespace layout and phone sync are not verified.','Known source degradations are retained; expectations use the confirmed target model.']};
}
async function run(request){return runBatch({scopes:[request],cookies:request.cookies})}
module.exports={validScope,allowRequest,classifyBlockedRequest,inspectEditor,requestRoute,bounded,prepareRouter,run,runBatch};
if(require.main===module){
 let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
 process.stdin.on('end',async()=>{
  try{const request=JSON.parse(input);input='';console.log(JSON.stringify(await(request.scopes?runBatch(request):run(request))))}
  catch{console.log(JSON.stringify({kind:'huawei-exact-target-native',status:'blocked',code:'scope',cloud_writes:0,formal_acceptance:false}))}
 });
}
