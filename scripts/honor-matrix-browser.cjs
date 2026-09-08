/* Read-only native renderer checks, scoped to a confirmed synthetic receipt.
 * DOM/contracts: .reference/official/honor/application/note-app.js, locally inspected.
 * No editor actions, dispatch calls, full-page screenshots, or credential output.
 */
const official=host=>host==='honor.com'||host.endsWith('.honor.com')||host==='hihonor.com'||host.endsWith('.hihonor.com');
const readGets=new Set(['/','/portal/notepad','/portal/user/info','/portal/config/web',
 '/portal/authorization/status','/portal/authorization/heartBeatCheck','/portal/user/application/status',
 '/portal/notepad/note/count','/portal/notepad/file/singleFileDownstream']);
const readPosts=new Set(['/portal/notepad/note/initDataBase/getFolderList',
 '/portal/notepad/note/util/getNoteList','/portal/notepad/noteDetail']);

function allowRead(method,address,resourceType,body){
  const url=new URL(address);
  if(url.protocol!=='https:'||!official(url.hostname)||/(?:delete|remove|create|insert|update|save|upload|commit|logout|signout|report|telemetry|analytics)/i.test(url.pathname))return false;
  // Official note-app awaits useUsage() before loading the folder/note page.
  if(url.pathname==='/portal/user/usage')return method==='GET'&&url.origin==='https://cloud.honor.com';
  // Web initDatebase dispatches this read without arguments before showing folders.
  if(url.pathname==='/portal/notepad/initDataBase/initGetFullPageData')return method==='POST'&&
   url.origin==='https://cloud.honor.com'&&!url.search&&(body===undefined||body===null||body==='');
  if(method==='POST')return url.hostname==='cloud.honor.com'&&readPosts.has(url.pathname);
 if(!['GET','HEAD'].includes(method))return false;
 return readGets.has(url.pathname)||(['script','stylesheet','image','font'].includes(resourceType)&&
  /\.(?:js|css|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf)$/i.test(url.pathname));
}

function validateScope(request){
 if(!/^[a-f0-9]{32}$/.test(request.fixtureId)||typeof request.title!=='string'||!request.title.startsWith('笔记互迁验收 · '))throw Error('scope');
 if(typeof request.expectedText!=='string'||!request.expectedText.trim()||request.expectedText.length>100000)throw Error('content_scope');
 const binding=request.default_group_binding;
 if(binding&&!(binding.fixture==='fixture-20260906-oppo-image-F2'&&/^[a-f0-9]{64}$/.test(binding.source_fingerprint)&&
  binding.target_id===request.fixtureId&&binding.target_group_id===request.group_id))throw Error('group_scope');
 const groupPattern=binding?/^未分类(?: \(\d+\))?$/:/^笔记互迁分组验收 [A-Z0-9 -]+(?: \(\d+\)){0,8}$/;
 if((request.group_name||binding)&&(!groupPattern.test(request.group_name)||request.group_name.length>50))throw Error('group_scope');
 if(request.group_name&&!/^[a-f0-9]{32}$/.test(request.group_id))throw Error('group_scope');
 if(!Number.isInteger(request.expected_images)||request.expected_images<0||request.expected_images>30||
  !Array.isArray(request.image_specs)||request.image_specs.length!==request.expected_images||
  new Set(request.image_specs.map(image=>image.id)).size!==request.expected_images||
  request.image_specs.some(image=>typeof image.id!=='string'||!image.id||!Number.isInteger(image.width)||image.width<=0||!Number.isInteger(image.height)||image.height<=0))throw Error('image_scope');
 for(const key of ['expected_styles','headings','todos','lists'])if(!Array.isArray(request[key]))throw Error('content_scope');
}

async function openSession(cookies){
 let browser;const blocked=[];
 try{
  const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN',serviceWorkers:'block'});
  await context.route('**/*',route=>{
   const req=route.request();
   if(allowRead(req.method(),req.url(),req.resourceType(),req.postData()))return route.continue();
   const url=new URL(req.url());
   // Paths/query strings can contain identifiers or signed data; return only route classes.
   const entry={method:req.method(),officialHost:official(url.hostname),resourceType:req.resourceType(),
    knownReadPath:readGets.has(url.pathname)||readPosts.has(url.pathname),
    pathClass:/^\/portal\/(?:authorization|config|user|notepad)\/(?:[a-zA-Z]+\/)*[a-zA-Z]+$/.test(url.pathname)?url.pathname:'other'};
   if(!blocked.some(item=>JSON.stringify(item)===JSON.stringify(entry)))blocked.push(entry);
   return route.abort();
  });
  await context.addCookies(cookies);
  return {browser,context,page:await context.newPage(),blocked,navigated:false};
 }catch(error){if(browser)await browser.close();throw error;}
 finally{if(Array.isArray(cookies))cookies.length=0;}
}

function failure(error,stage,diagnostics=[],blocked=[]){
 const known=['scope','content_scope','group_scope','image_scope','batch_scope','ambiguous_group','group_identity_missing','ambiguous_identity','identity_missing'];
 return {kind:'honor-matrix-native',formal_acceptance:false,cloud_writes:0,status:'blocked',stage,
  code:known.includes(error.message)?error.message:error.name,diagnostics,blocked:[...blocked]};
}

function selectedGroupListReady(scope){
 const visible=node=>{const box=node.getBoundingClientRect();return box.width>0&&box.height>0;};
 const propsFor=(node,name)=>{
  const key=Object.keys(node).find(k=>k.startsWith('__reactFiber$'));
  for(let fiber=node[key],depth=0;fiber&&depth<16;fiber=fiber.return,depth++){
   if(fiber.memoizedProps?.[name])return fiber.memoizedProps[name];
  }
  return null;
 };
 const groups=[...document.querySelectorAll('[data-note-bridge-group="matched"]')].filter(visible);
 if(groups.length!==1||!groups[0].classList.contains('active')||propsFor(groups[0],'data')?.key!==scope.group_id||
  groups[0].querySelector('.noteFolderTitle')?.textContent.trim()!==scope.group_name)return false;
 // SELECT_FOLDER updates the highlight before refreshNotes resolves. The previous
 // group's short list must not be mistaken for a fully loaded list with no match.
 const rows=[...document.querySelectorAll('.cardlist')].filter(visible);
 return rows.length>0&&rows.every(node=>propsFor(node,'note')?.folder_uuid===scope.group_id);
}

async function inspect(request,session){
 let stage='navigate';const diagnostics=[],{page,blocked}=session;
 try{
  if(!session.navigated){
   await page.goto('https://cloud.honor.com/portal/notepad',{waitUntil:'domcontentloaded',timeout:30000});
   session.navigated=true;
  }
  // Local inspection markers and list position must not leak from a previous item.
  await page.evaluate(()=>{
   document.querySelectorAll('[data-note-bridge-group]').forEach(node=>node.removeAttribute('data-note-bridge-group'));
   document.querySelectorAll('[data-note-bridge-fixture]').forEach(node=>node.removeAttribute('data-note-bridge-fixture'));
   document.querySelectorAll('.cardlistDiv').forEach(node=>{node.scrollTop=0;});
  });
  let groupSelected=!request.group_name;
  if(request.group_name){
   stage='select_group';
   await page.locator('.noteTreeNodeItem').first().waitFor({state:'visible',timeout:20000});
   const groups=await page.locator('.noteTreeNodeItem:visible').evaluateAll((nodes,scope)=>{
    let matches=0;
    for(const node of nodes){
     const key=Object.keys(node).find(k=>k.startsWith('__reactFiber$'));
     for(let fiber=node[key],depth=0;fiber&&depth<16;fiber=fiber.return,depth++){
      const data=fiber.memoizedProps?.data;
      if(data?.key===scope.group_id&&node.querySelector('.noteFolderTitle')?.textContent.trim()===scope.group_name){
       node.setAttribute('data-note-bridge-group','matched');matches++;break;
      }
     }
    }
    return matches;
   },request);
   if(groups!==1)throw Error(groups?'ambiguous_group':'group_identity_missing');
   const group=page.locator('[data-note-bridge-group="matched"]');
   await group.scrollIntoViewIfNeeded();await group.click();
   stage='group_list_ready';
   await page.waitForFunction(selectedGroupListReady,request,{timeout:20000});
   groupSelected=true;
  }
  stage='select_identity';
  await page.locator('.cardlist').first().waitFor({state:'visible',timeout:20000});
  let located=false;
  for(let attempt=0;attempt<30&&!located;attempt++){
   const proof=await page.locator('.cardlist:visible').evaluateAll((nodes,scope)=>{
    let matches=0;
    for(const node of nodes){
     const key=Object.keys(node).find(k=>k.startsWith('__reactFiber$'));
     for(let fiber=node[key],depth=0;fiber&&depth<16;fiber=fiber.return,depth++){
      const note=fiber.memoizedProps?.note;
      if(note?.uuid===scope.fixtureId){
       if(note.title!==scope.title||note.folder_uuid!==scope.group_id)throw Error('row_scope_mismatch');
       node.setAttribute('data-note-bridge-fixture','matched');matches++;break;
      }
     }
    }
    return {matches,rows:nodes.length};
   },request);
   if(proof.matches>1)throw Error('ambiguous_identity');
   if(proof.matches===1){located=true;break;}
   const moved=await page.locator('.cardlistDiv').evaluateAll(nodes=>{
    let moved=false;for(const node of nodes){const old=node.scrollTop;node.scrollTop+=node.clientHeight*0.75;moved||=node.scrollTop>old;}return moved;
   });
   if(!moved){diagnostics.push(proof);break;}
   await page.waitForTimeout(200);
  }
  if(!located)throw Error('identity_missing');
  await page.locator('[data-note-bridge-fixture="matched"]').click();
  stage='editor_identity';
  await page.waitForFunction(scope=>{
   const root=document.querySelector('#richtextContainer');if(!root)return false;
   const key=Object.keys(root).find(k=>k.startsWith('__reactFiber$'));
   for(let fiber=root[key],depth=0;fiber&&depth<30;fiber=fiber.return,depth++){
    const props=fiber.memoizedProps;
    if(props?.noteId===scope.fixtureId&&props.provisionalNoteInfo?.uuid===scope.fixtureId&&
       props.provisionalNoteInfo?.folder_uuid===scope.group_id&&props.provisionalNoteInfo?.title===scope.title)return true;
   }
   return false;
  },request,{timeout:20000});
  stage='native_content';
  const editor=page.locator('#richtextContainer #_hinote-stage:visible');
  await editor.waitFor({state:'visible',timeout:15000});
  await page.waitForFunction(scope=>{
   const node=document.querySelector('#richtextContainer #_hinote-stage');
   const compact=value=>value.replace(/\s/g,'');
   return node&&compact(node.innerText).includes(compact(scope.expectedText));
  },request,{timeout:15000});
  const result=await editor.evaluate(async(node,scope)=>{
   const visible=el=>el.getBoundingClientRect().width>0&&el.getBoundingClientRect().height>0;
   const compact=value=>value.replace(/\s/g,'');
   const images=[...node.querySelectorAll('.Ni[hid] img')];
   await Promise.all(images.map(image=>image.decode().catch(()=>{})));
   const imageProof=images.map(image=>{
    const hid=image.closest('.Ni[hid]').getAttribute('hid');
    const expected=scope.image_specs.find(spec=>spec.id===hid);
    return {identityMatched:!!expected&&images.filter(other=>other.closest('.Ni[hid]').getAttribute('hid')===hid).length===1,
     decoded:image.naturalWidth>0&&image.naturalHeight>0,visible:visible(image),
     ratioMatches:!!expected&&image.naturalHeight>0&&Math.abs(image.naturalWidth/image.naturalHeight-expected.width/expected.height)<0.03};
   });
   const fonts=[...node.querySelectorAll('h-text font')];
   const styles=scope.expected_styles.map((expected,index)=>{
    const matches=fonts.filter(font=>compact(font.textContent)===compact(expected.text));
    const observations=matches.map(font=>{
     const css=getComputedStyle(font);
     return {bold:Number(css.fontWeight)>=600,italic:css.fontStyle==='italic',
      underline:css.textDecorationLine.includes('underline')||(font.classList.contains('Lc')&&parseFloat(css.borderBottomWidth)>0&&css.borderBottomStyle==='solid'&&!['transparent','rgba(0, 0, 0, 0)'].includes(css.borderBottomColor)),
      strike:css.textDecorationLine.includes('line-through'),
      highlight:!['transparent','rgba(0, 0, 0, 0)'].includes(css.backgroundColor),
      linkMatched:!expected.link||font.getAttribute('hlink')===expected.link,visible:visible(font)};
    });
    const matchesStyle=observed=>observed.visible&&observed.linkMatched&&['bold','italic','underline','strike','highlight'].every(key=>!expected[key]||observed[key]);
    return {index,occurrences:matches.length,verified:observations.length>0&&observations.every(matchesStyle),observations};
   });
   const headings=scope.headings.map(expected=>[...node.querySelectorAll('[hlevel]')].some(el=>Number(el.getAttribute('hlevel'))===expected.level&&compact(el.innerText)===compact(expected.text)&&visible(el)));
   const todos=scope.todos.map(expected=>[...node.querySelectorAll('.Na')].some(el=>compact([...el.querySelectorAll('h-text')].map(n=>n.innerText).join(''))===compact(expected.text)&&
    (el.classList.contains('Sc')||!!el.querySelector('.Sc'))===expected.checked&&visible(el)));
   const lists=scope.lists.map(expected=>[...node.querySelectorAll('.Nc')].some(el=>compact(el.innerText)===compact(expected.text)&&!!el.closest(expected.ordered?'.Nd':'.Nh')&&visible(el)));
   return {bodyTextMatches:compact(node.innerText).includes(compact(scope.expectedText)),textComparison:'full ordered text excluding whitespace',
    bodyCharacters:node.innerText.length,images:imageProof,styles,headings,todos,lists};
  },request);
  const verified=groupSelected&&result.bodyTextMatches&&result.images.length===request.expected_images&&
   result.images.every(item=>Object.values(item).every(Boolean))&&result.styles.every(item=>item.verified)&&
   [...result.headings,...result.todos,...result.lists].every(Boolean);
  return {kind:'honor-matrix-native',formal_acceptance:false,cloud_writes:0,status:verified?'content_images_styles_verified':'needs_review',
   identity_matched:true,title_matched:true,group_identity_matched:true,groupSelected,...result,blocked:[...blocked],
   limitations:request.limitations||[]};
 }catch(error){
  return failure(error,stage,diagnostics,blocked);
 }
}

async function run(request){
 let session,stage='scope';
 try{
  validateScope(request);stage='starting';session=await openSession(request.cookies);
  return await inspect(request,session);
 }catch(error){return failure(error,stage);}
 finally{if(Array.isArray(request.cookies))request.cookies.length=0;if(session)await session.browser.close();}
}

async function runBatch(request){
 let session,stage='scope';const items=[];
 try{
  if(!Array.isArray(request.scopes)||request.scopes.length<1||request.scopes.length>30||
   new Set(request.scopes.map(scope=>scope.fixtureId)).size!==request.scopes.length)throw Error('batch_scope');
  request.scopes.forEach(validateScope);
  if(request.scopes.some(scope=>Object.hasOwn(scope,'cookies')))throw Error('batch_scope');
  stage='starting';session=await openSession(request.cookies);
  for(const [index,scope] of request.scopes.entries()){
   const report=await inspect(scope,session);items.push({index,...report});
   // An unconfirmed identity/session is never followed by more selections in that page.
   if(report.status==='blocked')break;
  }
  return {kind:'honor-matrix-native-batch',formal_acceptance:false,cloud_writes:0,
   status:items.length===request.scopes.length&&items.every(item=>item.status==='content_images_styles_verified')?'verified':'needs_review',
   total:request.scopes.length,checked:items.length,remaining:request.scopes.length-items.length,items};
 }catch(error){return {...failure(error,stage),kind:'honor-matrix-native-batch',items};}
 finally{if(Array.isArray(request.cookies))request.cookies.length=0;if(session)await session.browser.close();}
}

module.exports={allowRead,validateScope,selectedGroupListReady,run,runBatch};
if(require.main===module){
 let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>input+=chunk);
 process.stdin.on('end',async()=>{
  try{const request=JSON.parse(input);input='';console.log(JSON.stringify(await (Object.hasOwn(request,'scopes')?runBatch(request):run(request))));}
  catch{console.log(JSON.stringify({kind:'honor-matrix-native',formal_acceptance:false,cloud_writes:0,status:'blocked',code:'request_invalid'}));}
 });
}
