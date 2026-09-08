/* Receipt-scoped official WPS inspection; no cloud mutation routes are allowed. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>input+=chunk);
process.stdin.on('end',async()=>{
 let browser,stage='scope';const blocked=[],diagnostics=[];
 try{
  const request=JSON.parse(input);input='';
  if(!/^[a-f0-9]{32}$/.test(request.fixtureId)||!request.title.startsWith('笔记互迁验收 · '))throw Error('scope');
  if(typeof request.expectedText!=='string'||!request.expectedText||request.expectedText.length>100000)throw Error('content_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  const reads=new Set(['/notesvr/get/notegroup','/notesvr/get/web-config','/notesvr/get/notebody','/notesvr/web/getnotes/group','/s3/requestdownload']);
  await context.route('**/*',route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method();
   if((['GET','HEAD'].includes(method)&&!/(?:delete|remove|create|insert|update|save|upload|commit|logout)/i.test(url.pathname))
      ||(method==='POST'&&url.hostname==='note-api.wps.cn'&&reads.has(url.pathname)))return route.continue();
   blocked.push({method,host:url.hostname,path:url.pathname});return route.abort();
  });
  await context.addCookies(request.cookies);request.cookies.length=0;
  const page=await context.newPage();stage='navigate';
  await page.goto('https://note.wps.cn/',{waitUntil:'domcontentloaded',timeout:30000});
  const dismiss=page.locator('.migration-dialog:not(.migration-copy-dialog) .migration-dialog__secondary-btn');
  stage='locate_identity';let located=false,groupSelected=!request.group_name;
  const deadline=Date.now()+30000;
  while(Date.now()<deadline&&!located){
   if(await dismiss.isVisible().catch(()=>false))await dismiss.click();
   if(!groupSelected){
    const count=await page.locator('.group_item_title:visible').evaluateAll((nodes,name)=>{
     let matches=0;for(const node of nodes)if(node.textContent.trim()===name){node.setAttribute('data-note-bridge-group','matched');matches++;}return matches;
    },request.group_name);
    if(count===1){const group=page.locator('[data-note-bridge-group="matched"]');await group.scrollIntoViewIfNeeded();await group.click();groupSelected=true;}
    else if(count>1)throw Error('ambiguous_group');
    else{await page.waitForTimeout(500);continue;}
   }
   const proof=await page.locator('.note_list_item:visible').evaluateAll((nodes,id)=>{
    let matched=0;
    for(const node of nodes){
     const vm=node.__vue__;
     if(vm&&String(vm.$props?.noteInfo?.noteId)===id){node.setAttribute('data-note-bridge-fixture','matched');matched++;}
    }
    return {rows:nodes.length,matched};
   },request.fixtureId);
   if(proof.matched===1){located=true;break;}
   if(proof.matched>1)throw Error('ambiguous_identity');
   await page.waitForTimeout(500);
  }
  if(!located){diagnostics.push({rows:await page.locator('.note_list_item').count(),matchingTitle:await page.getByText(request.title,{exact:false}).count()});throw Error('identity_missing');}
  await page.locator('[data-note-bridge-fixture="matched"] .list-box').click();
  stage='editor_identity_and_content';let ready=false;
  const editor=page.locator('.ql-editor');
  const contentDeadline=Date.now()+20000;
  while(Date.now()<contentDeadline&&!ready){
   if(await editor.count()===1){
    ready=await editor.evaluate((node,scope)=>{
     let identity=false;
     for(let el=node;el;el=el.parentElement){if(String(el.__vue__?.$store?.state?.content?.noteId)===scope.fixtureId){identity=true;break;}}
     const compact=text=>text.replace(/\s/g,'');
     return identity&&compact(node.innerText).includes(compact(scope.expectedText));
    },request);
   }
   if(!ready)await page.waitForTimeout(500);
  }
  if(!ready)throw Error('editor_content_unconfirmed');
  const result=await editor.evaluate(async(node,scope)=>{
   const images=[...node.querySelectorAll('img.e-img')];await Promise.all(images.map(img=>img.decode().catch(()=>{})));
   const samples={};
   for(const label of ['加粗','斜体','下划线','删除线','高亮','组合样式 😀','链接文字','已完成验收项','未完成验收项','第一项','第二项']){
    samples[label]=[];const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);let text;
    while((text=walker.nextNode())){
     if(text.textContent.trim()!==label)continue;
     const sample={bold:false,italic:false,underline:false,strike:false,highlight:false,ancestors:[]};
     for(let el=text.parentElement;el&&el!==node;el=el.parentElement){
      const css=getComputedStyle(el);sample.bold ||= parseInt(css.fontWeight,10)>=600;sample.italic ||= css.fontStyle==='italic';
      sample.underline ||= css.textDecorationLine.includes('underline');sample.strike ||= css.textDecorationLine.includes('line-through');
      sample.highlight ||= el.tagName==='MARK'&&!['transparent','rgba(0, 0, 0, 0)'].includes(css.backgroundColor);
      sample.ancestors.push({tag:el.tagName,classes:[...el.classList],attributes:Object.fromEntries(['data-checked','checked','aria-checked'].filter(k=>el.hasAttribute(k)).map(k=>[k,el.getAttribute(k)]))});
     }
     samples[label].push(sample);
    }
   }
   const knownAddress='https://example.com/note-bridge';
   const knownLinks=[...node.querySelectorAll('a')].filter(a=>a.getAttribute('href')===knownAddress);
   const addressNodes=[...node.querySelectorAll('*')].filter(el=>el.children.length===0&&el.textContent.includes(knownAddress)).map(el=>({tag:el.tagName,classes:[...el.classList],ancestors:[el,el.parentElement,el.parentElement?.parentElement].filter(Boolean).map(a=>({tag:a.tagName,classes:[...a.classList],attributes:[...a.attributes].map(v=>({name:v.name,exactAddress:v.value===knownAddress,includesAddress:v.value.includes(knownAddress)}))}))}));
   const linkProof={expectedAddressInScope:scope.expectedText.includes(knownAddress),addressInBody:node.innerText.includes(knownAddress),exactHrefCount:knownLinks.length,visibleHrefCount:knownLinks.filter(a=>a.getBoundingClientRect().width>0&&a.getBoundingClientRect().height>0).length,addressNodes};
   return {bodyTextMatches:true,textComparison:'full ordered text excluding whitespace',bodyCharacters:node.innerText.length,linkProof,images:images.map(img=>({decoded:img.naturalWidth>0&&img.naturalHeight>0,visible:img.getBoundingClientRect().width>0,ratio:img.naturalHeight?img.naturalWidth/img.naturalHeight:0})),samples};
  },request);
  const verified=result.images.length===request.expected_images&&result.images.every(i=>i.decoded&&i.visible&&Math.abs(i.ratio-640/240)<0.03);
  console.log(JSON.stringify({kind:'wps-matrix-native',formal_acceptance:false,cloud_writes:0,status:verified?'content_images_verified':'needs_review',identity_matched:true,groupSelected,...result,blocked}));
 }catch(error){const known=['scope','content_scope','ambiguous_group','ambiguous_identity','identity_missing','editor_content_unconfirmed'];console.log(JSON.stringify({kind:'wps-matrix-native',formal_acceptance:false,cloud_writes:0,status:'blocked',stage,code:known.includes(error.message)?error.message:error.name,diagnostics,blocked}));}
 finally{if(browser)await browser.close();}
});
