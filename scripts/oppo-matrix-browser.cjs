/* Official OPPO renderer, exact receipted IDs; no editor commands or login automation. */
const API=/^owork(?:0[1-9])?-api-cn\.oppo\.com$/;
// Official VUE_APP_ALB_URL / $getBaseOrigin().albBaseOrigin for image URLs.
const IMAGE_ORIGIN='https://owork-alb-cn.oppo.com';
// Official editor preview and note-list thumbnail transforms, respectively.
const THUMBNAIL_TRANSFORMS=new Set(['image/format,webp/quality,Q_85','image/resize,h_42/quality,Q_60/format,webp']);
const PREFIX='/owork-server/web/note/v2/';
// Official host router mounts the notes micro-app here. /note is its share route.
const ENTRY='https://cloud.oppo.com/owork/mapp/sticky-notes';
function validScope(s){
 const label=/^(oppo-complex-OR1:seed|(?:wps|xiaomi|honor|meizu|huawei|vivo)-oppo-BI(?:[1-9]|1[0-2]):[01])$/;
 const expectedImages=/^xiaomi-oppo-BI(?:[1-9]|1[0-2]):0$/.test(s?.scopeLabel||'')?0:2;
 return s?.scopeVersion===1&&label.test(s.scopeLabel||'')&&/^[A-Za-z0-9_-]{1,150}$/.test(s.fixtureId||'')
  &&/^[A-Za-z0-9_-]{1,150}$/.test(s.group_id||'')&&typeof s.group_name==='string'
  &&typeof s.title==='string'&&s.title.startsWith('笔记互迁验收 · ')
  &&typeof s.expectedText==='string'&&s.expectedText.trim().length>0&&s.expectedText.length<=100000
  &&(s.expected_version_sha256===undefined||(typeof s.expected_version_sha256==='string'&&/^[0-9a-f]{64}$/.test(s.expected_version_sha256)))
  &&Array.isArray(s.expectedRuns)&&s.expectedRuns.length<=10000&&s.expectedRuns.every(r=>typeof r.text==='string')
  &&['headings','todos','lists','quotes','codes','tables'].every(k=>Array.isArray(s[k]))
  &&Number.isInteger(s.dividers)&&s.dividers>=0
  &&s.image_specs?.length===expectedImages&&new Set(s.image_specs.map(i=>i.id)).size===expectedImages
  &&s.image_specs.every(i=>typeof i.id==='string'&&i.id&&/^[A-Za-z0-9_-]{1,150}$/.test(i.cloud_id||'')
   &&Number.isInteger(i.width)&&i.width>0&&Number.isInteger(i.height)&&i.height>0);
}
function allowRequest(method,raw,resourceType,payload,ids,files){
 let url;try{url=new URL(raw)}catch{return false}
 const path=url.pathname;
 if(url.protocol!=='https:'||/(?:create|add|update|delete|remove|upload|merge|commit|save|insert|logout|login|signin|signout|report|telemetry|analytics)/i.test(path))return false;
 if(method==='POST'){
  if(!API.test(url.hostname)||!payload||Object.keys(payload).sort().join(',')!=='encryptContent,iv,key'
   ||Object.values(payload).some(v=>typeof v!=='string'))return false;
  if(path===PREFIX+'info')return ids.has(url.searchParams.get('recordId'))&&
   [...url.searchParams.keys()].every(k=>['recordId','version'].includes(k));
  return ['group-list-new','group-note-count','list'].some(end=>path===PREFIX+end)
   ||path==='/owork-server/web/paint_note/v2/list';
 }
 if(!['GET','HEAD'].includes(method))return false;
 if(url.origin===IMAGE_ORIGIN&&!url.username&&!url.password)return (
  (path.startsWith(PREFIX+'file-download/')&&files.has(path.slice((PREFIX+'file-download/').length))&&!url.search)
  ||(path.startsWith(PREFIX+'file-thumbnail/')&&files.has(path.slice((PREFIX+'file-thumbnail/').length))
   &&[...url.searchParams.keys()].join(',')==='x-kit-process'
   &&THUMBNAIL_TRANSFORMS.has(url.searchParams.get('x-kit-process'))));
 if(url.hostname==='id.heytap.com')return resourceType==='script'&&path==='/packages/account_web_sdk/index.umd.js'&&!url.search;
 const staticType=['script','stylesheet','font','image'].includes(resourceType);
 // Qiankun appends INDEX=index.html and fetches JS/CSS before executing them.
 if(API.test(url.hostname)&&((['/note/','/note/index.html','/note/navigator.html'].includes(path)&&['xhr','fetch'].includes(resourceType)&&!url.search)
  ||(staticType&&/^\/note\/(?:static\/|favicon\.ico$)/.test(path))
  ||(['xhr','fetch'].includes(resourceType)&&/^\/note\/static\/[^?#]+\.(?:js|css)$/.test(path)&&!url.search)))return true;
 if(API.test(url.hostname))return path==='/owork-server/web/account/v1/userInfo'
  ||(path.startsWith(PREFIX+'file-download/')&&files.has(path.slice((PREFIX+'file-download/').length)))
  ||(path.startsWith(PREFIX+'file-thumbnail/')&&files.has(path.slice((PREFIX+'file-thumbnail/').length))
   &&[...url.searchParams.keys()].join(',')==='x-kit-process'
   &&THUMBNAIL_TRANSFORMS.has(url.searchParams.get('x-kit-process')));
 if(url.hostname!=='cloud.oppo.com')return false;
 if(resourceType==='document')return ['/','/note','/note/','/note/index.html','/owork/mapp/sticky-notes'].includes(path)&&!url.search;
 return staticType&&(/^\/(?:note|owork)\/static\//.test(path)||['/note/favicon.ico','/favicon.ico'].includes(path)||/^\/conf\//.test(path));
}
function routeClass(raw){
 try{const p=new URL(raw).pathname;
  if(p.startsWith(PREFIX+'file-download/'))return 'selected_image';
  if(p.startsWith(PREFIX+'file-thumbnail/'))return 'selected_thumbnail';
  for(const k of ['info','group-list-new','group-note-count','list'])if(p===PREFIX+k)return k;
  if(p==='/owork-server/web/paint_note/v2/list')return 'handwriting_count';
  if(p==='/owork-server/web/account/v1/userInfo')return 'account';
 }catch{}
 return 'other';
}
function blockedRequest(req,kind,budgetExceeded){
 const url=new URL(req.url());
 return {method:['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'].includes(req.method())?req.method():'other',
  pathClass:kind,officialHost:API.test(url.hostname)||url.hostname==='cloud.oppo.com'||url.origin===IMAGE_ORIGIN,budgetExceeded,
  hostClass:url.hostname==='cloud.oppo.com'?'cloud':API.test(url.hostname)?'api':url.hostname==='id.heytap.com'?'account':
   url.origin===IMAGE_ORIGIN?'file_alb':url.hostname==='static-o.oppo.com'?'static_cdn':'other',
  resourceType:['document','script','stylesheet','font','image','xhr','fetch'].includes(req.resourceType())?req.resourceType():'other',
  hasQuery:!!url.search,pathFingerprint:require('node:crypto').createHash('sha256').update(url.pathname).digest('hex')};
}
function bootstrapClass(raw){
 try{const url=new URL(raw);if(url.protocol!=='https:'||!API.test(url.hostname)||url.search)return null;
  if(['/note/','/note/index.html','/note/navigator.html'].includes(url.pathname))return 'entry';
  if(/^\/note\/static\/[^?#]+\.(?:js|css)$/.test(url.pathname))return 'asset';
 }catch{}
 return null;
}
function accountResponse(status,payload,parsed=null){
 // Official current client en._isInvalidAuth recognizes only numeric 1003/1004.
 const valid=parsed===true&&payload!==null&&typeof payload==='object'&&!Array.isArray(payload);
 const identity=valid?payload.data?.ssoId:undefined;
 return {httpStatus:Number.isInteger(status)&&status>=100&&status<=599?status:null,
  jsonParsed:parsed,codeZero:parsed===null?null:valid&&(payload.code===0||payload.code==='0'),
  ssoIdPresent:parsed===null?null:valid&&['string','number'].includes(typeof identity)&&!!identity,
  authRejected:[401,403].includes(status)||(valid&&[1003,1004].includes(payload.code))};
}
// Vue 2 component ownership is taken from the saved official listItem/richEditorVue
// contracts. Reading props/state never invokes component methods or editor commands.
function locateRow(scope){
 let matches=0;const rows=[...document.querySelectorAll('li.list-item')];
 for(const row of rows){
  row.removeAttribute('data-note-bridge-selected');
  if(!row.getBoundingClientRect().width)continue;
  const seen=new Set();let match=false;
  for(const node of [row,...row.querySelectorAll('*')]){
   let vm=node.__vue__;
   for(let depth=0;vm&&depth<8;depth++,vm=vm.$parent){
    if(seen.has(vm))break;seen.add(vm);
    const option=vm.$props?.optionData;
    if(option?.recordId===scope.fixtureId){
     if(option.rawTitle!==scope.title||option.groupGuid!==scope.group_id||option.status!==0)return {matches:0,wrongRow:true};
     if(vm.$el&&row.contains(vm.$el))match=true;
    }
   }
  }
  if(match){matches++;row.setAttribute('data-note-bridge-selected','true');}
 }
 return {matches,rows:rows.length};
}
function pageDiagnostics(scope){
 // Only counts and fixed enums leave the page; never serialize component state.
 const visible=n=>!!n?.isConnected&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0;
 const lists=[...document.querySelectorAll('.list-container ul.list')].filter(visible),owners=new Set();
 for(const list of lists)for(let el=list;el;el=el.parentElement){
  const seen=new Set();
  for(let vm=el.__vue__,depth=0;vm&&depth<12;vm=vm.$parent,depth++){
   if(seen.has(vm))break;seen.add(vm);
   if(Array.isArray(vm.dataListNotes?.list)&&vm.$refs?.scrollContainer?.contains(list))owners.add(vm);
  }
 }
 const rows=[...document.querySelectorAll('li.list-item')],number=n=>Number.isInteger(n)&&n>=0&&n<=1000000?n:null;
 const path=location.pathname,host=location.hostname;
 const result={readyState:['loading','interactive','complete'].includes(document.readyState)?document.readyState:'other',
  hostClass:host==='cloud.oppo.com'?'cloud':/^owork(?:0[1-9])?-api-cn\.oppo\.com$/.test(host)?'api':host==='id.heytap.com'?'account':'other',
  pathClass:path==='/owork/mapp/sticky-notes'?'note_list':/^\/note(?:\/|$)/.test(path)?'note_share':path==='/'?'portal':'other',
  appPresent:!!document.getElementById('app'),listContainers:lists.length,listOwners:owners.size,
  appContainerPresent:!!document.getElementById('appContainer'),qiankunStarted:globalThis.isQiankunStart===true,
  rows:rows.length,visibleRows:rows.filter(visible).length,editors:[...document.querySelectorAll('.ProseMirror')].filter(visible).length};
 if(owners.size===1){
  const owner=[...owners][0],data=owner.dataListNotes;
  Object.assign(result,{loadedRows:number(data.list.length),pageNo:number(data.pageNo),pageSize:number(data.pageSize),
   totalCount:number(data.totalCount),hasMore:typeof data.hasMore==='boolean'?data.hasMore:null,
   loading:typeof owner.loading==='boolean'?owner.loading:null,
   selectedInLoadedList:data.list.some(note=>note.recordId===scope.fixtureId)});
 }
 return result;
}
async function inspectEditor(scope){
 const compact=v=>String(v??'').replace(/\s/g,'');
 const visible=n=>n?.isConnected&&n.getBoundingClientRect().width>0&&n.getBoundingClientRect().height>0;
 const editors=[...document.querySelectorAll('.ProseMirror')].filter(visible);
 const checks={uniqueEditor:editors.length===1,identity:false,group:false,title:false,ownedDOM:false,
  modelText:false,renderedText:false,styles:false,structures:false,images:false};
 const diagnostics={editors:editors.length,owners:0};
 if(editors.length!==1)return {checks,diagnostics};
 const node=editors[0],owners=[],seen=new Set();
 for(let element=node;element;element=element.parentElement){
  for(let vm=element.__vue__,depth=0;vm&&depth<15;vm=vm.$parent,depth++){
   if(seen.has(vm))break;seen.add(vm);
   if(vm.editor?.view?.dom===node&&vm.noteDetail&&vm.$el?.contains(node))owners.push(vm);
  }
 }
 diagnostics.owners=owners.length;
 if(owners.length!==1)return {checks,diagnostics};
 const owner=owners[0],detail=owner.noteDetail;
 let parent=owner.$parent;
 while(parent&&parent.$refs?.richEditorVueRef!==owner)parent=parent.$parent;
 checks.identity=detail.recordId===scope.fixtureId&&detail.status===0&&parent?.currentNote?.recordId===scope.fixtureId;
 if(scope.expected_version_sha256!==undefined){
  // Match Python digest(version): an ensure_ascii JSON string, including quotes.
  const version=detail.version,encoded=typeof version==='string'?JSON.stringify(version).replace(/[\u007f-\uffff]/g,
   character=>'\\u'+character.charCodeAt(0).toString(16).padStart(4,'0')):null;
  const hash=encoded===null?null:[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(encoded)))]
   .map(byte=>byte.toString(16).padStart(2,'0')).join('');
  diagnostics.versionMatched=hash===scope.expected_version_sha256&&parent?.currentNote?.version===version;
  checks.identity=checks.identity&&diagnostics.versionMatched;
 }
 checks.group=detail.groupGuid===scope.group_id&&parent?.currentNote?.groupGuid===scope.group_id&&
  (!scope.group_name||owner.groupName===scope.group_name);
 diagnostics.titleDetailMatched=detail.rawTitle===scope.title;
 diagnostics.titleParentMatched=parent?.currentNote?.rawTitle===scope.title;
 checks.title=diagnostics.titleDetailMatched&&diagnostics.titleParentMatched;
 checks.ownedDOM=owner.editor.view.dom===node&&owner.editor.view.dom.isConnected;
 if(!checks.identity||!checks.group||!checks.title||!checks.ownedDOM)return {checks,diagnostics};
 const doc=owner.editor.state?.doc;
 // Official 401e converts a div-wrapped rawTitle to H1, but a plain rawTitle
 // remains a text prefix and parses as a paragraph. The official paragraph
 // extension renders DIV. Bind the complete, separate title node; never remove
 // a matching prefix from a body node.
 const titleNode=node.firstElementChild,titleModel=doc?.firstChild;
 diagnostics.titleDOMTag=['H1','P','DIV'].includes(titleNode?.tagName)?titleNode.tagName:'other';
 diagnostics.titleModelType=['heading','paragraph'].includes(titleModel?.type?.name)?titleModel.type.name:'other';
 diagnostics.titleModelLevel=Number.isInteger(titleModel?.attrs?.level)&&titleModel.attrs.level>=1&&titleModel.attrs.level<=6?titleModel.attrs.level:null;
 diagnostics.titleDOMMatched=compact(titleNode?.innerText)===compact(scope.title);
 diagnostics.titleModelMatched=compact(titleModel?.textContent)===compact(scope.title);
 diagnostics.titleProjectionMatched=(titleNode?.tagName==='H1'&&titleModel?.type?.name==='heading'&&titleModel.attrs?.level===1)
  ||(['P','DIV'].includes(titleNode?.tagName)&&titleModel?.type?.name==='paragraph');
 checks.title=checks.title&&diagnostics.titleProjectionMatched&&diagnostics.titleDOMMatched&&diagnostics.titleModelMatched;
 if(!checks.title)return {checks,diagnostics};
 checks.modelText=!!doc&&typeof doc.textBetween==='function'&&
  compact(doc.textBetween(titleModel.nodeSize,doc.content.size,'\n',''))===compact(scope.expectedText);
 // The official underline extension renders a 1.3px gradient strip on U and
 // explicitly resets text-decoration. Require its visible strip AND the mark
 // on this exact model text range; a background or another U is insufficient.
 const officialUnderline=(element,css,textNode)=>{
  if(element.tagName!=='U'||!visible(element)||css.visibility!=='visible'||css.display==='none'||Number(css.opacity)<=0||
   css.backgroundSize!=='100% 1.3px'||css.backgroundRepeat!=='no-repeat'||
   !['0% 100%','100% 100%'].includes(css.backgroundPosition)||css.paddingBottom!=='3px')return false;
  for(let parent=element.parentElement;parent;parent=parent.parentElement){
   const style=getComputedStyle(parent);
   if(style.visibility!=='visible'||style.display==='none'||Number(style.opacity)<=0)return false;
   if(parent===node)break;
  }
  const gradient=/^linear-gradient\(90deg,\s*(rgba?\([\d.,\s]+\)),\s*\1\)$/.exec(css.backgroundImage||'');
  if(!gradient)return false;
  const color=gradient[1].match(/[\d.]+/g).map(Number);
  if(![3,4].includes(color.length)||color.slice(0,3).some(value=>!Number.isFinite(value)||value<0||value>255)||
   (color.length===4&&!(color[3]>0&&color[3]<=1)))return false;
  if(typeof owner.editor.view.posAtDOM!=='function'||typeof doc.nodesBetween!=='function')return false;
  try{
   const from=owner.editor.view.posAtDOM(textNode,0),to=from+textNode.textContent.length;
   if(!Number.isInteger(from)||from<titleModel.nodeSize||to>doc.content.size||
    doc.textBetween(from,to,'','')!==textNode.textContent)return false;
   let covered=0;
   doc.nodesBetween(from,to,(model,position)=>{
    if(model.isText&&model.marks?.some(mark=>mark.type.name==='underline'&&mark.attrs?.type==='solid'))
     covered+=Math.max(0,Math.min(to,position+model.nodeSize)-Math.max(from,position));
   });
   return covered===textNode.textContent.length;
  }catch{return false;}
 };
 const shown=[],walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);let text;
 while((text=walker.nextNode())){
  if(titleNode.contains(text)||text.parentElement.closest('button,svg,[role="button"],.image-node-view-wrapper'))continue;
  const flags={bold:false,italic:false,underline:false,strike:false,highlight:false,code:false,link:null};
  for(let el=text.parentElement;el;el=el.parentElement){
   const css=getComputedStyle(el);
   flags.bold ||= parseInt(css.fontWeight,10)>=600;flags.italic ||= css.fontStyle==='italic';
   flags.underline ||= css.textDecorationLine.includes('underline')||officialUnderline(el,css,text);
   flags.strike ||= css.textDecorationLine.includes('line-through');
   flags.highlight ||= el!==node&&!['transparent','rgba(0, 0, 0, 0)','rgb(255, 255, 255)'].includes(css.backgroundColor);
   flags.code ||= el.tagName==='CODE'||/monospace/i.test(css.fontFamily);
   if(el.tagName==='A'&&flags.link===null)flags.link=el.getAttribute('href');
   if(el===node)break;
  }
  for(const ch of text.textContent)if(!/\s/.test(ch))shown.push({ch,flags});
 }
 const expected=scope.expectedRuns.flatMap(run=>[...run.text].filter(ch=>!/\s/.test(ch)).map(ch=>({ch,run})));
 checks.renderedText=shown.map(v=>v.ch).join('')===compact(scope.expectedText);
 const styleKeys=['bold','italic','underline','strike','highlight','code','link'];
 diagnostics.styleTextAligned=shown.length===expected.length&&shown.every((v,i)=>v.ch===expected[i].ch);
 diagnostics.styleExpectedCharacters=expected.length;diagnostics.styleRenderedCharacters=shown.length;
 diagnostics.styleExpectedCounts={};diagnostics.styleMatchedCounts={};diagnostics.styleChecks={};
 for(const key of styleKeys){
  const required=expected.filter(e=>!!e.run[key]).length;
  const matched=expected.filter((e,i)=>e.run[key]&&shown[i]?.ch===e.ch&&
   (key==='link'?shown[i].flags.link===e.run.link:shown[i].flags[key])).length;
  diagnostics.styleExpectedCounts[key]=required;diagnostics.styleMatchedCounts[key]=matched;
  diagnostics.styleChecks[key]=required===matched;
 }
 checks.styles=checks.renderedText&&diagnostics.styleTextAligned&&Object.values(diagnostics.styleChecks).every(Boolean);
 const equal=(actual,wanted)=>JSON.stringify(actual)===JSON.stringify(wanted);
 const elements=selector=>[...node.querySelectorAll(selector)].filter(element=>element!==titleNode&&!titleNode.contains(element));
 const headings=elements('h1,h2,h3,h4,h5,h6').map(n=>({text:compact(n.innerText),level:Number(n.tagName.slice(1))}));
 // taskItem's live NodeView supplies data-checked and a checkbox, but does not
 // copy the serialized data-type attribute. Bind both controls and model state.
 const todoModels=[];
 if(typeof doc.descendants==='function')doc.descendants(model=>{if(model.type.name==='taskItem')todoModels.push(model)});
 const todoNodes=elements('ul[data-type="taskList"] li[data-checked]');
 const todos=todoNodes.map(n=>({text:compact(n.innerText),checked:n.getAttribute('data-checked')==='true'}));
 const todosBound=todoModels.length===todos.length&&todoNodes.every((n,i)=>{
  const input=n.querySelector(':scope > label > input[type="checkbox"]'),model=todoModels[i];
  return ['true','false'].includes(n.getAttribute('data-checked'))&&input?.checked===todos[i].checked&&
   model.attrs.checked===todos[i].checked&&compact(model.textContent)===todos[i].text;
 });
 const ownedNodeModel=(element,wrapper,type)=>{
  if(!wrapper)return null;
  const models=new Set(),seen=new Set();
  for(let el=element;el&&el!==node;el=el.parentElement){
   // Vue's NodeViewWrapper is a child of the component holding node props.
   for(let vm=el.__vue__,depth=0;vm&&depth<10;vm=vm.$parent,depth++){
    if(seen.has(vm))break;seen.add(vm);
    const model=vm.$props?.node||vm.node;
    if(model?.type?.name===type&&vm.$el?.contains(element)&&
     (vm.$el===wrapper||wrapper.contains(vm.$el)))models.add(model);
   }
   if(el===wrapper)break;
  }
  return models.size===1?[...models][0]:null;
 };
 const dividerNodes=elements('.divider'),dividerTypes=['solid','dashed','double','freehand','short-dashed','short-freehand'];
 const dividersBound=dividerNodes.every(element=>{
  const model=ownedNodeModel(element,element,'divider'),type=model?.attrs?.lineType;
  return dividerTypes.includes(type)&&visible(element)&&!!element.querySelector('.hr-style-'+type);
 });
 const lists=elements('li').filter(n=>!n.closest('ul[data-type="taskList"]')).map(n=>({text:compact(n.innerText),ordered:n.parentElement.tagName==='OL'}));
 const structures={headings:equal(headings,scope.headings.map(v=>({...v,text:compact(v.text)}))),
  todos:todosBound&&equal(todos,scope.todos.map(v=>({...v,text:compact(v.text)}))),
  lists:equal(lists,scope.lists.map(v=>({...v,text:compact(v.text)}))),
  quotes:equal(elements('blockquote').map(n=>compact(n.innerText)),scope.quotes.map(compact)),
  codes:equal(elements('pre').map(n=>compact(n.innerText)),scope.codes.map(compact)),
  tables:equal(elements('table').map(n=>[...n.rows].map(row=>[...row.cells].map(cell=>compact(cell.innerText)))),
    scope.tables.map(rows=>rows.map(row=>row.map(compact)))),dividers:dividersBound&&dividerNodes.length===scope.dividers};
 checks.structures=Object.values(structures).every(Boolean);
 const images=elements('img'),proof=[];
 // Pending downloads are revisited by the bounded readiness loop; do not wait
 // indefinitely inside decode() for an incomplete request.
 await Promise.all(images.filter(image=>image.complete).map(image=>image.decode().catch(()=>{})));
 for(const image of images){
  const wrapper=image.closest('.image-node-view-wrapper'),model=ownedNodeModel(image,wrapper,'image');
  const id=model?.attrs?.attachId!=null?String(model.attrs.attachId):null,match=scope.image_specs.find(spec=>spec.id===id);
  proof.push({identityMatched:!!match,decoded:image.complete===true&&image.naturalWidth>0&&image.naturalHeight>0,visible:!!visible(image),
   dimensionsMatched:!!match&&image.naturalWidth===match.width&&image.naturalHeight===match.height,
   scopeIndex:match?scope.image_specs.indexOf(match):-1});
 }
 checks.images=proof.length===scope.image_specs.length&&new Set(proof.map(p=>p.scopeIndex)).size===scope.image_specs.length&&proof.every(p=>
  p.identityMatched&&p.decoded&&p.visible&&p.dimensionsMatched);
 return {checks,diagnostics,structures,images:proof,bodyCharacters:shown.length,
  textComparison:'complete ordered model and rendered text excluding whitespace'};
}
async function runBatch(request){
 let browser,page,activeScope,stage='scope',pageErrors=0,failedRequests=0;const items=[],blocked=[],counts={};
 const accountResponses=[],bootstrap={entryRequested:0,entrySucceeded:0,assetRequested:0,assetSucceeded:0};
 const accountRejected=()=>accountResponses.some(response=>response.authRejected);
 const diagnostics=async()=>{
  let state={};if(page&&activeScope)try{state=await page.evaluate(pageDiagnostics,activeScope)}catch{}
  const requests={};for(const key of ['info','group-list-new','group-note-count','list','handwriting_count','account','selected_image','selected_thumbnail'])
   requests[key]=Object.entries(counts).filter(([name])=>name===key||name.startsWith(key+':')).reduce((total,[,count])=>total+count,0);
  return {...state,pageErrors,failedRequests,requests,accountResponses,bootstrap};
 };
 try{
  if(!Array.isArray(request.scopes)||request.scopes.length<1||request.scopes.length>2||
   request.scopes.some(s=>!validScope(s)||Object.hasOwn(s,'cookies'))||
   new Set(request.scopes.map(s=>s.fixtureId)).size!==request.scopes.length)throw Error('scope');
  const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN',serviceWorkers:'block'});
  const ids=new Set(request.scopes.map(s=>s.fixtureId)),files=new Set(request.scopes.flatMap(s=>s.image_specs.map(i=>i.cloud_id)));
  await context.route('**/*',route=>{
   const req=route.request(),kind=routeClass(req.url());let payload=null;
   try{payload=req.postDataJSON()}catch{}
   let allowed=allowRequest(req.method(),req.url(),req.resourceType(),payload,ids,files);
   const url=new URL(req.url()),key=kind==='info'?kind+':'+url.searchParams.get('recordId'):kind;
   const limit=kind==='list'?3:kind==='handwriting_count'?1:kind==='info'?2:kind==='account'?3:kind==='group-list-new'?3:kind==='group-note-count'?30:['selected_image','selected_thumbnail'].includes(kind)?8:Infinity;
   if(allowed&&kind!=='other'){counts[key]=(counts[key]||0)+1;allowed=counts[key]<=limit;}
   if(allowed)return route.continue();
   const entry=blockedRequest(req,kind,(counts[key]||0)>limit);
   if(!blocked.some(x=>JSON.stringify(x)===JSON.stringify(entry)))blocked.push(entry);
   return route.abort();
  });
  await context.addCookies(request.cookies);request.cookies.length=0;
  page=await context.newPage();stage='navigate';activeScope=request.scopes[0];
  page.on('pageerror',()=>pageErrors++);page.on('requestfailed',()=>failedRequests++);
  page.on('request',req=>{const kind=bootstrapClass(req.url());if(kind&&req.method()==='GET')bootstrap[kind+'Requested']++;});
  page.on('response',response=>{
   const req=response.request(),kind=bootstrapClass(response.url());
   if(kind&&req.method()==='GET'&&response.status()===200)bootstrap[kind+'Succeeded']++;
   let url;try{url=new URL(response.url())}catch{return}
   if(req.method()!=='GET'||url.protocol!=='https:'||!API.test(url.hostname)||url.pathname!=='/owork-server/web/account/v1/userInfo'||accountResponses.length>=3)return;
   const entry=accountResponse(response.status(),undefined);accountResponses.push(entry);
   response.json().then(payload=>Object.assign(entry,accountResponse(response.status(),payload,true)))
    .catch(()=>Object.assign(entry,accountResponse(response.status(),undefined,false)));
  });
  await page.goto(ENTRY,{waitUntil:'domcontentloaded',timeout:30000});
  for(const [index,scope] of request.scopes.entries()){
   stage='select_identity';activeScope=scope;let selected=false;
   // Qiankun asynchronously fetches its entry and assets after the host DOM is ready.
   for(let attempt=0;attempt<(index===0?180:60)&&!selected;attempt++){
    if(accountRejected())throw Error('account_rejected');
    const row=await page.evaluate(locateRow,scope);
    if(row.wrongRow||row.matches>1)throw Error('ambiguous_identity');
    if(row.matches===1){selected=true;break;}
    await page.waitForTimeout(250);
   }
   if(accountRejected())throw Error('account_rejected');
   if(!selected){const state=await page.evaluate(pageDiagnostics,scope);throw Error(state.listContainers?'identity_missing':'initialization_timeout');}
   await page.locator('li[data-note-bridge-selected="true"]').click();
   stage='editor_content';let result;
   for(let attempt=0;attempt<60;attempt++){
    if(accountRejected())throw Error('account_rejected');
    result=await page.evaluate(inspectEditor,scope);
    if(Object.values(result.checks).every(Boolean))break;
    await page.waitForTimeout(250);
   }
   const after=await page.evaluate(inspectEditor,scope);
   if(accountRejected())throw Error('account_rejected');
   const verified=Object.values(result.checks).every(Boolean)&&Object.values(after.checks).every(Boolean);
   items.push({index,status:verified?'content_images_styles_verified':'needs_review',...after});
   if(!verified)break;
  }
  return {kind:'oppo-matrix-native-batch',formal_acceptance:false,cloud_writes:0,
   status:items.length===request.scopes.length&&items.every(i=>i.status==='content_images_styles_verified')?'verified':'needs_review',
   total:request.scopes.length,checked:items.length,remaining:request.scopes.length-items.length,items,blocked,
   diagnostics:await diagnostics()};
 }catch(error){
  return {kind:'oppo-matrix-native-batch',formal_acceptance:false,cloud_writes:0,status:'blocked',stage,
   code:['scope','ambiguous_identity','identity_missing','account_rejected','initialization_timeout'].includes(error.message)?error.message:error.name,items,blocked,
   diagnostics:await diagnostics()};
 }finally{if(Array.isArray(request.cookies))request.cookies.length=0;if(browser)await browser.close();}
}
module.exports={ENTRY,validScope,allowRequest,routeClass,blockedRequest,bootstrapClass,accountResponse,locateRow,pageDiagnostics,inspectEditor,runBatch};
if(require.main===module){
 let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>input+=chunk);
 process.stdin.on('end',async()=>{
  try{const request=JSON.parse(input);input='';console.log(JSON.stringify(await runBatch(request)));}
  catch{console.log(JSON.stringify({kind:'oppo-matrix-native-batch',status:'blocked',code:'request_invalid',cloud_writes:0,formal_acceptance:false}));}
 });
}
