/* Receipt-scoped Flyme native inspection. DOM/API evidence: locally saved
 * .reference/official/meizu/current/script-0.js: k7e, ePe, Q7e, e7e and YFe.
 * All cloud writes and unobserved requests are blocked, including editor autosave.
 */
const official=host=>['flyme.cn','meizu.com','mzres.com'].some(domain=>host===domain||host.endsWith('.'+domain));
const readPosts=new Set(['/c/browser/note/gettags','/c/browser/note/getnotegroups','/c/browser/note/getnotebycontent']);
function allowRead(method,address,resourceType){
 const url=new URL(address);
 if(url.protocol!=='https:'||!official(url.hostname)||/(?:delete|remove|addFile|addNode|create|insert|update|save|upload|moveNote|batchTop|recycle|recover|logout|report|tongji|telemetry|analytics)/i.test(url.pathname))return false;
 if(method==='POST')return url.hostname==='notes.flyme.cn'&&readPosts.has(url.pathname);
 if(!['GET','HEAD'].includes(method))return false;
 return (url.hostname==='notes.flyme.cn'&&['/','/c/login/getLoginInfo'].includes(url.pathname))||
  (['script','stylesheet','image','font'].includes(resourceType)&&/\.(?:js|css|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf)$/i.test(url.pathname));
}
function validateScope(scope){
 if(!scope||typeof scope!=='object'||!/^[a-f0-9]{32}$/.test(scope.fixtureId)||typeof scope.title!=='string'||!scope.title.startsWith('笔记互迁验收 · '))throw Error('scope');
 if(typeof scope.expectedText!=='string'||!scope.expectedText.trim()||scope.expectedText.length>100000)throw Error('content_scope');
 const binding=scope.default_group_binding;
 if(binding&&!(binding.fixture==='fixture-20260906-oppo-image-F2'&&/^[a-f0-9]{64}$/.test(binding.source_fingerprint)&&
  binding.target_id===scope.fixtureId&&binding.target_group_id===scope.group_id))throw Error('group_scope');
 const groupPattern=binding?/^未分类(?: \(\d+\))?$/:/^笔记互迁分组验收 [A-Z0-9 -]+(?: \(\d+\)){0,4}(?: \(\d* \(\d+\))?$/;
 if((scope.group_name||binding)&&(!groupPattern.test(scope.group_name)||scope.group_name.length>16))throw Error('group_scope');
 if(!/^(?:[a-f0-9]{32}|-1)$/.test(scope.group_id)||(scope.group_name&&scope.group_id==='-1'))throw Error('group_scope');
 if(!Number.isInteger(scope.expected_images)||scope.expected_images<0||scope.expected_images>30||
  !Array.isArray(scope.image_specs)||scope.image_specs.length!==scope.expected_images||
  new Set(scope.image_specs.map(image=>image.id)).size!==scope.expected_images||
  scope.image_specs.some(image=>typeof image.id!=='string'||!image.id||!Number.isInteger(image.width)||image.width<=0||!Number.isInteger(image.height)||image.height<=0))throw Error('image_scope');
 for(const key of ['expected_styles','headings','todos','lists'])if(!Array.isArray(scope[key]))throw Error('content_scope');
}
function failure(error,stage,diagnostics=[],blocked=[]){
 const known=['scope','content_scope','group_scope','image_scope','batch_scope','group_identity_missing','ambiguous_group','identity_missing','ambiguous_identity','privacy_confirmation_required'];
 return {kind:'meizu-matrix-native',formal_acceptance:false,cloud_writes:0,status:'blocked',stage,
  code:known.includes(error.message)?error.message:error.name,diagnostics,blocked:[...blocked]};
}
async function openSession(cookies){
 let browser;const blocked=[];
 try{
  const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN',serviceWorkers:'block'});
  await context.route('**/*',route=>{
   const request=route.request();
   if(allowRead(request.method(),request.url(),request.resourceType()))return route.continue();
   const url=new URL(request.url()),entry={method:request.method(),officialHost:official(url.hostname),resourceType:request.resourceType(),
    pathClass:/^\/c\/(?:browser\/note|login)\/[a-zA-Z]+$/.test(url.pathname)?url.pathname:'other'};
   if(!blocked.some(item=>JSON.stringify(item)===JSON.stringify(entry)))blocked.push(entry);
   return route.abort();
  });
  await context.addCookies(cookies);
  return {browser,context,page:await context.newPage(),blocked,navigated:false};
 }catch(error){if(browser)await browser.close();throw error;}
 finally{if(Array.isArray(cookies))cookies.length=0;}
}
function initialPageState(){
 const visible=node=>{if(!node)return false;const box=node.getBoundingClientRect();return box.width>0&&box.height>0;};
 // WPe renders this local agreement gate instead of k7e's sidebar in a new context.
 // Observe it only: accepting terms or copying localStorage is outside inspection.
 const privacy=document.querySelector('.privacy-text');
 if(visible(privacy)&&privacy.textContent.includes('《笔记隐私政策》')&&privacy.textContent.includes('《笔记服务协议》'))return 'privacy_confirmation_required';
 return visible(document.querySelector('.sidebar'))?'ready':false;
}
async function inspect(scope,session){
 let stage='navigate';const {page,blocked}=session,diagnostics=[];
 try{
  if(!session.navigated){await page.goto('https://notes.flyme.cn/',{waitUntil:'domcontentloaded',timeout:30000});session.navigated=true;}
  await page.evaluate(()=>{
   document.querySelectorAll('[data-note-bridge-group]').forEach(node=>node.removeAttribute('data-note-bridge-group'));
   document.querySelectorAll('[data-note-bridge-fixture]').forEach(node=>node.removeAttribute('data-note-bridge-fixture'));
  });
  stage='wait_sidebar';
  const initial=await page.waitForFunction(initialPageState,{},{timeout:20000});
  let initialState;try{initialState=await initial.jsonValue();}finally{await initial.dispose();}
  diagnostics.push({page_state:initialState});
  if(initialState==='privacy_confirmation_required')throw Error(initialState);
  stage='select_group';
  let groupSelected=!scope.group_name;
  if(scope.group_name){
   const count=await page.locator('.group-list .row').evaluateAll((nodes,expected)=>{
    let matches=0;for(const node of nodes){
     const key=Object.keys(node).find(k=>k.startsWith('__reactFiber$'));
     if(node[key]?.key===expected.group_id&&node.querySelector('.name')?.textContent.trim()===expected.group_name){node.setAttribute('data-note-bridge-group','matched');matches++;}
    }return matches;
   },scope);
   if(count!==1)throw Error(count?'ambiguous_group':'group_identity_missing');
   const group=page.locator('[data-note-bridge-group="matched"]');await group.scrollIntoViewIfNeeded();await group.click();
   await page.waitForFunction(()=>document.querySelector('[data-note-bridge-group="matched"]')?.classList.contains('selected'),{},{timeout:15000});
   groupSelected=true;
  }else{
   // k7e's first direct sidebar row selects all notes; it cannot create a group.
   await page.locator('.sidebar > .row').first().click();
  }
  stage='select_identity';
  await page.locator('.catalogue-row').first().waitFor({state:'visible',timeout:20000});
  const matched=await page.locator('.catalogue-row').evaluateAll((nodes,expected)=>{
   let matches=0;
   for(const row of nodes){
    const key=Object.keys(row).find(k=>k.startsWith('__reactFiber$'));
    if(row[key]?.key!==expected.fixtureId)continue;
    const summary=row.querySelector('.summary');if(!summary)continue;
    const summaryKey=Object.keys(summary).find(k=>k.startsWith('__reactFiber$'));
    for(let fiber=summary[summaryKey],depth=0;fiber&&depth<8;fiber=fiber.return,depth++){
     const item=fiber.memoizedProps?.item;
     if(item?.uuid===expected.fixtureId&&item.title===expected.title&&(item.groupStatus||'-1')===expected.group_id){
      row.setAttribute('data-note-bridge-fixture','matched');matches++;break;
     }
    }
   }return matches;
  },scope);
  if(matched!==1)throw Error(matched?'ambiguous_identity':'identity_missing');
  await page.locator('[data-note-bridge-fixture="matched"]').click();
  stage='editor_identity';
  await page.waitForFunction(expected=>{
   const selected=document.querySelector('[data-note-bridge-fixture="matched"].highlight');
   const content=document.querySelector('.editor-content'),dom=content?.querySelector('.ProseMirror');
   if(!selected||!content||!dom)return false;
   let editor=null;
   const key=Object.keys(content).find(k=>k.startsWith('__reactFiber$'));
   for(let fiber=content[key],depth=0;fiber&&depth<16;fiber=fiber.return,depth++){
    const candidate=fiber.memoizedProps?.editor;
    if(candidate&&!candidate.isDestroyed&&candidate.view?.dom===dom){editor=candidate;break;}
   }
   const root=content.closest('.content');if(!root||!editor)return false;
   const rootKey=Object.keys(root).find(k=>k.startsWith('__reactFiber$'));
   const pending=[root[rootKey]?.child];let visited=0;
   while(pending.length&&visited++<500){
    const fiber=pending.pop();if(!fiber)continue;
    const props=fiber.memoizedProps;
    if(props?.editor===editor&&props.selectedNoteUUID===expected.fixtureId)return true;
    pending.push(fiber.child,fiber.sibling);
   }return false;
  },scope,{timeout:20000});
  stage='native_content';
  const editor=page.locator('.editor-content .ProseMirror');
  await page.waitForFunction(expected=>{
   const node=document.querySelector('.editor-content .ProseMirror'),compact=text=>text.replace(/\s/g,'');
   return node&&compact(node.innerText).includes(compact(expected.expectedText));
  },scope,{timeout:15000});
  const result=await editor.evaluate(async(node,expected)=>{
   const compact=text=>text.replace(/\s/g,''),visible=element=>element.getBoundingClientRect().width>0&&element.getBoundingClientRect().height>0;
   const images=[...node.querySelectorAll('.image-wapper > img')];await Promise.all(images.map(image=>image.decode().catch(()=>{})));
   const imageProof=images.map(image=>{
    const identity=image.getAttribute('alt'),item=expected.image_specs.find(spec=>spec.id===identity);
    return {identityMatched:!!item&&images.filter(other=>other.getAttribute('alt')===identity).length===1,
     decoded:image.naturalWidth>0&&image.naturalHeight>0,visible:visible(image),
     ratioMatches:!!item&&image.naturalHeight>0&&Math.abs(image.naturalWidth/image.naturalHeight-item.width/item.height)<0.03};
   });
   const texts=[],walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);let text;
   while((text=walker.nextNode()))texts.push(text);
   const styles=expected.expected_styles.map((sample,index)=>{
    const matches=texts.filter(item=>compact(item.textContent)===compact(sample.text));
    const observations=matches.map(item=>{
     const proof={bold:false,italic:false,underline:false,strike:false,highlight:false,linkMatched:!sample.link,visible:visible(item.parentElement)};
     for(let element=item.parentElement;element&&element!==node;element=element.parentElement){
      const css=getComputedStyle(element);proof.bold||=Number(css.fontWeight)>=600;proof.italic||=css.fontStyle==='italic';
      proof.underline||=css.textDecorationLine.includes('underline');proof.strike||=css.textDecorationLine.includes('line-through');
      proof.highlight||=(element.tagName==='MARK'||element.style.backgroundColor)&&!['transparent','rgba(0, 0, 0, 0)'].includes(css.backgroundColor);
      proof.linkMatched||=!!sample.link&&element.tagName==='A'&&element.getAttribute('href')===sample.link;
     }return proof;
    });
    return {index,occurrences:matches.length,verified:observations.length>0&&observations.every(item=>item.visible&&item.linkMatched&&
     ['bold','italic','underline','strike','highlight'].every(key=>!sample[key]||item[key])),observations};
   });
   const headings=expected.headings.map(item=>[...node.querySelectorAll('h1,h2,h3,h4,h5,h6')].some(el=>Number(el.tagName.slice(1))===item.level&&compact(el.innerText)===compact(item.text)&&visible(el)));
   const todos=expected.todos.map(item=>[...node.querySelectorAll('li[data-type="taskItem"]')].some(el=>compact(el.innerText)===compact(item.text)&&(el.getAttribute('data-checked')==='true')===item.checked&&visible(el)));
   const lists=expected.lists.map(item=>[...node.querySelectorAll('li:not([data-type="taskItem"])')].some(el=>compact(el.innerText)===compact(item.text)&&el.parentElement.tagName===(item.ordered?'OL':'UL')&&visible(el)));
   return {bodyTextMatches:compact(node.innerText).includes(compact(expected.expectedText)),textComparison:'full ordered text excluding whitespace',
    bodyCharacters:node.innerText.length,images:imageProof,styles,headings,todos,lists};
  },scope);
  const verified=groupSelected&&result.bodyTextMatches&&result.images.length===scope.expected_images&&result.images.every(item=>Object.values(item).every(Boolean))&&
   result.styles.every(item=>item.verified)&&[...result.headings,...result.todos,...result.lists].every(Boolean);
  return {kind:'meizu-matrix-native',formal_acceptance:false,cloud_writes:0,status:verified?'content_images_styles_verified':'needs_review',
   identity_matched:true,title_matched:true,group_identity_matched:true,groupSelected,...result,blocked:[...blocked],limitations:scope.limitations||[]};
 }catch(error){return failure(error,stage,diagnostics,blocked);}
}
async function run(request){
 let session,stage='scope';
 try{validateScope(request);stage='starting';session=await openSession(request.cookies);return await inspect(request,session);}
 catch(error){return failure(error,stage);}
 finally{if(Array.isArray(request?.cookies))request.cookies.length=0;if(session)await session.browser.close();}
}
async function runBatch(request){
 let session,stage='scope';const items=[];
 try{
  if(!Array.isArray(request.scopes)||request.scopes.length<1||request.scopes.length>30||
   new Set(request.scopes.map(scope=>scope?.fixtureId)).size!==request.scopes.length)throw Error('batch_scope');
  request.scopes.forEach(validateScope);
  if(request.scopes.some(scope=>Object.hasOwn(scope,'cookies')))throw Error('batch_scope');
  stage='starting';session=await openSession(request.cookies);
  for(const [index,scope] of request.scopes.entries()){
   const report=await inspect(scope,session);items.push({index,...report});if(report.status==='blocked')break;
  }
  return {kind:'meizu-matrix-native-batch',formal_acceptance:false,cloud_writes:0,status:items.length===request.scopes.length&&items.every(item=>item.status==='content_images_styles_verified')?'verified':'needs_review',
   total:request.scopes.length,checked:items.length,remaining:request.scopes.length-items.length,items};
 }catch(error){return {...failure(error,stage),kind:'meizu-matrix-native-batch',items};}
 finally{if(Array.isArray(request?.cookies))request.cookies.length=0;if(session)await session.browser.close();}
}
module.exports={allowRead,validateScope,initialPageState,run,runBatch};
if(require.main===module){
 let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>input+=chunk);
 process.stdin.on('end',async()=>{
  try{const request=JSON.parse(input);input='';console.log(JSON.stringify(await(Object.hasOwn(request,'scopes')?runBatch(request):run(request))));}
  catch{console.log(JSON.stringify({kind:'meizu-matrix-native',formal_acceptance:false,cloud_writes:0,status:'blocked',code:'request_invalid'}));}
 });
}
