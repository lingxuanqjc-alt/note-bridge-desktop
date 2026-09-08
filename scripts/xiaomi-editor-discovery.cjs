/* Read-only official desktop entry inspection; no note content or credentials in output. */
function exactEditorProof(details,identity){
 // Official RichTextNotePc uses Ei2S's SUCCESS noteId/openNote and Gt2v's
 // read-only imperative getEditor().view.dom. Resolve the committed React tree:
 // a DOM __reactFiber pointer alone may still refer to its previous alternate.
 const proofs=[];
 const descendants=start=>{
  const result=[],pending=start?[start]:[];let remaining=30000;
  while(pending.length&&remaining--){
   const fiber=pending.pop();result.push(fiber);
   for(let child=fiber.child;child;child=child.sibling)pending.push(child);
  }
  return pending.length?[]:result;
 };
 for(const detail of details){
  for(const root of detail.querySelectorAll('[data-note-bridge-editor]'))root.removeAttribute('data-note-bridge-editor');
  const proof={currentTree:false,noteIdentityMatches:false,loadedCurrentNote:false,
   widgetOwnsDom:false,widgetReady:false,dataReceived:false,visible:false,matched:false};
  proofs.push(proof);
  const key=Object.keys(detail).find(key=>key.startsWith('__reactFiber$')||key.startsWith('__reactInternalInstance$'));
  let root=key?detail[key]:null;
  for(let depth=0;root?.return&&depth<100;depth++)root=root.return;
  const current=root?.stateNode?.current;
  if(!current)continue;
  const owners=descendants(current).filter(fiber=>fiber.stateNode===detail);
  if(owners.length!==1)continue;
  proof.currentTree=true;
  const owner=owners[0],notes=[];
  for(let fiber=owner,depth=0;fiber&&depth<100;fiber=fiber.return,depth++){
   const props=fiber.memoizedProps;
   if(props&&Object.prototype.hasOwnProperty.call(props,'noteId')&&props.note&&typeof props.note.get==='function')notes.push(props);
  }
  proof.noteIdentityMatches=notes.length>0&&notes.every(props=>String(props.noteId)===identity&&String(props.note.get('id'))===identity);
  proof.loadedCurrentNote=proof.noteIdentityMatches&&notes.every(props=>props.isLoading===false&&props.historyOpen===false&&props.locked===false&&props.isTypeCommon===true);
  if(!proof.loadedCurrentNote)continue;
  const handles=new Set(),widgets=[];
  for(const fiber of descendants(owner)){
   const handle=fiber.ref?.current;
   if(!handle||handles.has(handle)||typeof handle.getEditor!=='function'||typeof handle.isReady!=='function')continue;
   handles.add(handle);
   try{
    const widget=handle.getEditor(),dom=widget?.view?.dom;
    if(dom&&detail.contains(dom)&&dom.matches('.pm-container .ProseMirror[contenteditable="true"]'))widgets.push({handle,widget,dom});
   }catch{}
  }
  if(widgets.length!==1)continue;
  const {handle,widget,dom}=widgets[0];
  proof.widgetOwnsDom=true;proof.widgetReady=handle.isReady()===true;
  proof.dataReceived=widget.dataReceived===true;
  const box=dom.getBoundingClientRect();proof.visible=dom.isConnected===true&&box.width>0&&box.height>0;
  proof.matched=proof.widgetReady&&proof.dataReceived&&proof.visible;
  if(proof.matched)dom.setAttribute('data-note-bridge-editor','matched');
 }
 return {details:details.length,matched:proofs.filter(proof=>proof.matched).length,candidates:proofs};
}
function editorImageInventory(nodes){
 // Keep every image, including broken, hidden and unexpectedly sized images.
 return nodes.map(node=>({width:node.naturalWidth,height:node.naturalHeight,complete:node.complete,
  visible:node.getBoundingClientRect().width>0&&node.getBoundingClientRect().height>0,
  insideEditor:!!node.closest('[contenteditable=true]')}));
}
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser;const pending=[],assets=[],blocked=[],uploadContracts=[],updateContracts=[],locatorProofs=[];
 const fixtureImage=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNMibrDgA0wYRUdtBIAQ7sBqlteL2kAAAAASUVORK5CYII=','base64');
 try{
  const request=JSON.parse(input);input='';
  if(!['desktop-root','desktop-note','desktop-fixture','desktop-upload-contract','desktop-image-fixture','desktop-complex-fixture','desktop-update-contract','desktop-scoped-fixture'].includes(request.mode))throw Error('inspection_scope');
  const fixtureTitle=request.mode==='desktop-scoped-fixture'?request.fixtureTitle:request.mode==='desktop-complex-fixture'?'笔记互迁验收 · vivo综合 V1':['desktop-image-fixture','desktop-update-contract'].includes(request.mode)?'笔记互迁验收 · 小米双图片 AC6':'笔记互迁验收 · 小米分组 P1';
  if(request.mode==='desktop-scoped-fixture'&&(!/^\d{1,30}$/.test(request.fixtureId)||typeof fixtureTitle!=='string'||!fixtureTitle.startsWith('笔记互迁')))throw Error('fixture_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1360,height:900},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  await context.route('**/*',route=>{
   const req=route.request(),url=new URL(req.url());
   if(!['GET','HEAD'].includes(req.method())||/\/(?:delete|create|commit|upload|update)(?:\/|$)/i.test(url.pathname)){
    if(url.hostname==='i.mi.com'&&/^\/note\/note\/\d+\/?$/.test(url.pathname)){
     try{
      const form=new URLSearchParams(req.postData()||'');const entry=JSON.parse(form.get('entry')||'null');
      updateContracts.push({method:req.method(),formFields:[...form.keys()].filter(k=>['entry','serviceToken'].includes(k)),
       entryFields:entry&&Object.keys(entry).filter(k=>/^[A-Za-z_]{1,40}$/.test(k)),
       fieldTypes:entry&&Object.fromEntries(Object.entries(entry).filter(([k])=>/^[A-Za-z_]{1,40}$/.test(k)).map(([k,v])=>[k,Array.isArray(v)?'array':typeof v])),
       hasProbeText:!!entry?.content?.includes('笔记互迁本地请求核对 AG'),hasVersionTag:entry?.tag!==undefined,
       contentHasTitle:!!entry?.content?.includes(fixtureTitle),encryptInfoPresent:!!entry?.encryptInfo});
     }catch{updateContracts.push({parseFailed:true});}
    }
    if(url.hostname==='i.mi.com'&&url.pathname==='/file/v2/user/request_upload_file'){
     try{
      const form=new URLSearchParams(req.postData()||'');const data=JSON.parse(form.get('data')||'null');
      const storage=data?.storage;const blocks=storage?.kss?.block_infos;
      uploadContracts.push({independentMetadataMatches:require('node:util').isDeepStrictEqual(data,request.expectedUploadMetadata),typeMatches:data?.type==='note_img',filenameMatches:storage?.filename==='note-bridge-contract.png',
       sizeMatches:storage?.size===fixtureImage.length,mimeMatches:storage?.mimeType==='image/png',
       sha1Matches:storage?.sha1===crypto.createHash('sha1').update(fixtureImage).digest('hex'),
       encryptionPresent:form.has('encryptInfo'),blockCount:Array.isArray(blocks)?blocks.length:null,
       blocks:Array.isArray(blocks)?blocks.map(block=>({keys:Object.keys(block).filter(k=>['blob','size','md5','sha1','encryptedSha1'].includes(k)),
        sizeMatches:block.size===fixtureImage.length,md5Matches:block.md5===crypto.createHash('md5').update(fixtureImage).digest('hex'),
        sha1Matches:block.sha1===crypto.createHash('sha1').update(fixtureImage).digest('hex'),blobEmpty:JSON.stringify(block.blob)==='{}'})):[]});
     }catch{uploadContracts.push({parseFailed:true})}
    }
    blocked.push({method:req.method(),path:url.pathname.replace(/[0-9]{5,}/g,':id')});return route.abort();
   }
   return route.continue();
  });
  const directory=path.resolve(__dirname,'../.reference/official/xiaomi/'+request.mode);fs.mkdirSync(directory,{recursive:true});
  context.on('response',response=>{
   const url=new URL(response.url());
   if(response.request().resourceType()!=='script'||!url.hostname.endsWith('.mi-img.com')||url.search||response.status()!==200)return;
   pending.push((async()=>{
    const body=await response.body();if(body.length>8000000)return;
    const hash=crypto.createHash('sha256').update(body).digest('hex');
    fs.writeFileSync(path.join(directory,hash+'.js'),body);
    assets.push({host:url.hostname,path:url.pathname,sha256:hash,bytes:body.length});
   })().catch(()=>{}));
  });
  const page=await context.newPage();let navigation='loaded';
  const scoped=request.mode==='desktop-scoped-fixture';
  await page.goto(scoped?'https://i.mi.com/note/h5':'https://i.mi.com/',{waitUntil:'domcontentloaded',timeout:35000}).catch(()=>{navigation='pending'});
  await page.waitForTimeout(5000);
  if(request.mode!=='desktop-root'&&!scoped){
   const entry=page.getByText('笔记',{exact:true});
   if(await entry.count()!==1||!await entry.isVisible())throw Error('ambiguous_note_entry');
   await entry.click();
   await page.waitForTimeout(7000);
  }
  let fixtureOpened=false;
  if(['desktop-fixture','desktop-upload-contract','desktop-image-fixture','desktop-complex-fixture','desktop-update-contract','desktop-scoped-fixture'].includes(request.mode)){
   for(const openPage of context.pages())for(const frame of openPage.frames()){
    if(request.mode==='desktop-scoped-fixture'){
     const proof=await frame.locator('.note-item-3E9te').evaluateAll((nodes,identity)=>{
      let matched=0;
      for(const node of nodes){
       const key=Object.keys(node).find(key=>key.startsWith('__reactFiber$')||key.startsWith('__reactInternalInstance$'));
       let fiber=key?node[key]:null;
       for(let depth=0;fiber&&depth<8;depth++,fiber=fiber.return){
        const note=fiber.memoizedProps?.note;
        if(note&&typeof note.get==='function'&&String(note.get('id'))===identity){
         node.setAttribute('data-note-bridge-fixture','matched');matched++;break;
        }
       }
      }
      return {rows:nodes.length,matched};
     },request.fixtureId);
     locatorProofs.push(proof);
    }
    const fixture=request.mode==='desktop-scoped-fixture'?frame.locator('[data-note-bridge-fixture="matched"]'):frame.getByText(fixtureTitle,{exact:false});
    if(await fixture.count()===1&&await fixture.isVisible()){
     await fixture.click();fixtureOpened=true;await openPage.waitForTimeout(5000);
    }else if(request.mode!=='desktop-scoped-fixture'&&await frame.locator('[contenteditable=true]').evaluateAll((nodes,title)=>nodes.some(node=>node.textContent.includes(title)),fixtureTitle)){
     fixtureOpened=true;await openPage.waitForTimeout(5000);
    }
   }
  }
  if(request.mode==='desktop-upload-contract'){
   if(!fixtureOpened)throw Error('fixture_missing');
   const candidates=[];
   for(const openPage of context.pages())for(const frame of openPage.frames()){
    const control=frame.locator('button[data-name="Image"]:visible');
    if(await control.count()===1)candidates.push({openPage,control});
   }
   if(candidates.length!==1)throw Error('ambiguous_image_control');
   const {openPage,control}=candidates[0];
   const chooserPromise=openPage.waitForEvent('filechooser',{timeout:5000});
   await control.click();const chooser=await chooserPromise;
   await chooser.setFiles({name:'note-bridge-contract.png',mimeType:'image/png',buffer:fixtureImage});
   await openPage.waitForTimeout(6000);
  }
  if(request.mode==='desktop-update-contract'){
   if(!fixtureOpened)throw Error('fixture_missing');
   const candidates=[];
   for(const openPage of context.pages())for(const frame of openPage.frames()){
    const editor=frame.locator('[contenteditable=true]').filter({hasText:fixtureTitle});
    if(await editor.count()===1)candidates.push({openPage,editor});
   }
   if(candidates.length!==1)throw Error('ambiguous_editor');
   const {openPage,editor}=candidates[0];
   await editor.click();await editor.press('Control+End');await editor.press('Enter');
   await editor.pressSequentially('笔记互迁本地请求核对 AG');
   await openPage.waitForTimeout(7000);
  }
  const frames=[];
  for(const openPage of context.pages())for(const frame of openPage.frames()){
   const url=new URL(frame.url()||'about:blank');const labels={};
   for(const label of ['笔记','便签','云相册','全部笔记','图片','添加图片','新建笔记','登录','立即登录','笔记互迁验收 · 小米分组 P1'])
    labels[label]=await frame.getByText(label,{exact:true}).count().catch(()=>0);
   const imageControls=await frame.locator('button[data-name="Image"]').evaluateAll(nodes=>nodes.map(node=>({tag:node.tagName,disabled:node.disabled})));
   const editorProof=scoped?await frame.locator('.note-detail-2vftL').evaluateAll(exactEditorProof,request.fixtureId):null;
   const editorSelector=scoped?'[data-note-bridge-editor="matched"]':'[contenteditable=true]';
   const decodedImages=scoped?await frame.locator(editorSelector+' img').evaluateAll(editorImageInventory):
    await frame.locator('img').evaluateAll(nodes=>nodes.filter(node=>node.naturalWidth===640&&node.naturalHeight===240).map(node=>({width:node.naturalWidth,height:node.naturalHeight,complete:node.complete,visible:node.getBoundingClientRect().width>0&&node.getBoundingClientRect().height>0,insideEditor:!!node.closest('[contenteditable=true]')})));
   const fixtureTitleInEditor=await frame.locator(editorSelector).evaluateAll((nodes,title)=>nodes.some(node=>node.textContent.includes(title)),fixtureTitle);
   const styleSamples=fixtureTitleInEditor?await frame.locator(editorSelector).evaluateAll(roots=>{
    const result={};
    for(const label of ['删除线','高亮','链接文字','组合样式 😀','加粗','斜体','下划线']){
     result[label]=[];
     for(const root of roots){
      const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let textNode;
      while(textNode=walker.nextNode())if(textNode.nodeValue.includes(label)){
       let element=textNode.parentElement;const sample={strike:false,highlight:false,bold:false,italic:false,underline:false,link:false,underlineTrace:[]};
       while(element&&element!==root){
        const style=getComputedStyle(element),decoration=style.textDecorationLine;
        if(label==='下划线')sample.underlineTrace.push({tag:element.tagName,classes:[...element.classList],decoration,borderStyle:style.borderBottomStyle,borderWidth:style.borderBottomWidth,borderColor:style.borderBottomColor});
        sample.strike ||= decoration.includes('line-through');sample.underline ||= decoration.includes('underline');
        sample.underline ||= element.tagName==='U'&&style.borderBottomStyle==='solid'&&parseFloat(style.borderBottomWidth)>0&&!['transparent','rgba(0, 0, 0, 0)'].includes(style.borderBottomColor);
        sample.bold ||= parseInt(style.fontWeight,10)>=600;sample.italic ||= style.fontStyle==='italic';
        sample.highlight ||= !['transparent','rgba(0, 0, 0, 0)','rgb(255, 255, 255)'].includes(style.backgroundColor);
        sample.link ||= element.tagName==='A'&&element.href==='https://example.com/note-bridge';
        element=element.parentElement;
       }
       result[label].push(sample);
      }
     }
    }
    return result;
   }):{};
   const taskSamples=fixtureTitleInEditor?await frame.locator(editorSelector).evaluateAll(roots=>{
    const result={};
    for(const label of ['已完成验收项','未完成验收项','第一项','第二项']){
     result[label]=[];
     for(const root of roots){
      const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let node;
      while(node=walker.nextNode())if(node.nodeValue===label){
       let parent=node.parentElement,ancestors=[];
       while(parent&&parent!==root){
        ancestors.push({tag:parent.tagName,classes:[...parent.classList],
         attributes:Object.fromEntries([...parent.attributes].filter(a=>['role','checked','aria-checked','data-checked','data-type','data-indent','data-input-number'].includes(a.name)).map(a=>[a.name,a.value]))});
        parent=parent.parentElement;
       }
       result[label].push(ancestors);
      }
     }
    }
    return result;
   }):{};
   const linkCards=fixtureTitleInEditor?await frame.locator(editorSelector+' .link-card').evaluateAll(nodes=>nodes.map(node=>({
    titleMatches:(node.getAttribute('data-title')||node.querySelector('.title')?.textContent||'')==='链接文字',
    hrefMatches:(node.getAttribute('data-href')||node.querySelector('.link')?.textContent||'')==='https://example.com/note-bridge',
    visible:node.getBoundingClientRect().width>0&&node.getBoundingClientRect().height>0}))):[];
   const nativeStructures=fixtureTitleInEditor?await frame.locator(editorSelector).evaluateAll(roots=>({
    h2:roots.reduce((sum,root)=>sum+root.querySelectorAll('h2').length,0),
    quotes:roots.reduce((sum,root)=>sum+root.querySelectorAll('blockquote.quote').length,0),
    dividers:roots.reduce((sum,root)=>sum+root.querySelectorAll('hr').length,0)
   })):{};
   const editorProofAfter=scoped?await frame.locator('.note-detail-2vftL').evaluateAll(exactEditorProof,request.fixtureId):null;
   frames.push({host:url.hostname,path:url.pathname,labels,imageControls,decodedImages,fixtureTitleInEditor,editorProof,editorProofAfter,styleSamples,taskSamples,linkCards,nativeStructures,
               file_inputs:await frame.locator('input[type=file]').count(),
               editors:await frame.locator('[contenteditable=true],textarea').count()});
  }
  await Promise.allSettled(pending);
  const report={kind:'xiaomi-desktop-readonly-discovery',scopeVersion:scoped?2:null,requestedFixtureId:scoped?request.fixtureId:null,formal_acceptance:false,status:'inspected',navigation,fixtureOpened,locatorProofs,uploadContracts,updateContracts,frames,assets,blocked,cloud_writes:0};
  fs.writeFileSync(path.join(directory,'manifest.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }catch(error){const safeCodes=new Set(['inspection_scope','fixture_scope','ambiguous_note_entry','fixture_missing','ambiguous_image_control','ambiguous_editor']);console.log(JSON.stringify({kind:'xiaomi-desktop-readonly-discovery',formal_acceptance:false,status:'blocked',code:safeCodes.has(error.message)?error.message:error.name,cloud_writes:0}))}
 finally{if(browser)await browser.close()}
});
