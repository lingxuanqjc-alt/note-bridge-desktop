/* Inspect only a receipt-confirmed synthetic note; block all cloud mutation routes. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
let input='';process.stdin.on('data',chunk=>input+=chunk);
process.stdin.on('end',async()=>{
 let browser,stage='scope';const blocked=[],diagnostics=[],network=[];
 try{
  const request=JSON.parse(input);input='';
  if(!/^[a-f0-9]{32}$/.test(request.fixtureId)||!request.title.startsWith('笔记互迁验收 · '))throw Error('scope');
  if(!Number.isInteger(request.expected_images)||request.expected_images<0||request.expected_images>10)throw Error('images');
  if(typeof request.expectedText!=='string'||!request.expectedText||request.expectedText.length>100000)throw Error('content_scope');
  if(request.group_name&&!/^笔记互迁分组验收 [A-Z](?: \(\d{1,3}\)){0,6}$/.test(request.group_name))throw Error('group');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  context.on('response',async response=>{
   const url=new URL(response.url());
   if(url.hostname!=='pc.vivo.com.cn'||!url.pathname.startsWith('/note-api/'))return;
   const item={path:url.pathname,httpStatus:response.status()};
   try{const envelope=await response.json();if(typeof envelope.code==='number')item.code=envelope.code;}catch{}
   if(network.length<30)network.push(item);
  });
  const readPosts=new Set([
   '/note-api/account/getUserInfo','/note-api/account/getConfig','/note-api/sync/getSyncState',
   '/note-api/device/getList','/note-api/noteBook/getList','/note-api/statistics/note',
   '/note-api/note/getAllNote/v2','/note-api/note/getContent/v2','/note-api/history/note/list/v2',
   '/note-api/note/getNoteForNoteBook',
   '/note-api/sync/getSyncChunk/v2','/note-api/note/getIncludeItem/v2',
   '/clouddisk-api/api/suite/web/meta/getStsToken.do',
   '/clouddisk-api/api/suite/web/meta/fileDomain.do'
  ]);
  await context.route('**/*',route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method();
   const staticAsset=(url.hostname.endsWith('.vivo.com.cn')||url.hostname==='pc.vivo.com')&&url.pathname.startsWith('/suite/static/')&&/\.(?:js|css|png|svg|woff2?)$/.test(url.pathname);
   const read=['GET','HEAD'].includes(method)&&(staticAsset||!/(?:delete|remove|create|insert|update|save|upload|commit|logout)/i.test(url.pathname));
   if(read||(method==='POST'&&url.hostname==='pc.vivo.com.cn'&&readPosts.has(url.pathname)))return route.continue();
   blocked.push({method,host:url.hostname,path:url.pathname});return route.abort();
  });
  await context.addCookies(request.cookies);request.cookies.length=0;
  const page=await context.newPage();stage='navigate';
  await page.goto('https://pc.vivo.com.cn/suite?origin=cloudWeb',{waitUntil:'domcontentloaded',timeout:30000});
  const welcome=page.getByText('知道了',{exact:true});
  await welcome.waitFor({state:'visible',timeout:7000}).catch(()=>{});
  if(await welcome.count()===1&&await welcome.isVisible())await welcome.click();
  await page.getByText('原子笔记',{exact:true}).first().waitFor({state:'visible',timeout:15000});
  await page.getByText('原子笔记',{exact:true}).first().click();
  stage='select_identity';let selected=false,groupSelected=false;
  const deadline=Date.now()+20000;
  while(Date.now()<deadline&&!selected){
   for(const frame of context.pages().flatMap(p=>p.frames())){
    if(request.group_name&&!groupSelected){
     const expand=frame.locator('.open-folder.folder-hide:visible');
     if(await expand.count()===1){await expand.click();continue;}
     const group=frame.locator('.folder-container').getByText(request.group_name,{exact:true}).and(frame.locator(':visible'));
     if(await group.count()===1&&await group.isVisible()){
      groupSelected=await group.evaluate(el=>!!el.closest('.sl-vue-tree-selected'));
      if(!groupSelected){await group.click();await page.waitForTimeout(400);}
      continue;
     }
    }
    if(request.group_name&&!groupSelected)continue;
    for(const selector of [`[data-id="note-item-id-${request.fixtureId}"]`,`.note-container[data-guid="${request.fixtureId}"]`]){
     const item=frame.locator(selector);
     if(await item.count()===1){await item.scrollIntoViewIfNeeded();await item.click();selected=true;break;}
    }
    if(selected)break;
   }
   if(!selected)await page.waitForTimeout(400);
  }
  if(!selected){
   for(const frame of context.pages().flatMap(p=>p.frames()))diagnostics.push({
    host:new URL(frame.url()).hostname,
    noteRows:await frame.locator('[data-id^="note-item-id-"]').count(),
    noteContainers:await frame.locator('.note-container[data-guid]').count(),
    matchingTitle:await frame.getByText(request.title,{exact:true}).count(),groupSelected,
    groupMatches:request.group_name?await frame.getByText(request.group_name,{exact:true}).count():0,
    groupStructure:request.group_name?await frame.getByText(request.group_name,{exact:true}).evaluateAll(nodes=>nodes.map(node=>{const result=[];for(let el=node,depth=0;el&&depth<14;el=el.parentElement,depth++){const style=getComputedStyle(el),rect=el.getBoundingClientRect();result.push({tag:el.tagName,classes:[...el.classList],display:style.display,visibility:style.visibility,width:rect.width,height:rect.height,role:el.getAttribute('role'),children:[...el.children].slice(0,5).map(c=>({tag:c.tagName,classes:[...c.classList]}))});}return result;})):[],
    notebookNavigation:await frame.getByText('笔记本',{exact:true}).count(),
    allNotes:await frame.getByText('全部笔记',{exact:true}).count(),login:await frame.getByText('登录',{exact:true}).count()});
   throw Error('identity_not_found');
  }
  stage='read_editor';let editor;
  const editorDeadline=Date.now()+30000;
  while(Date.now()<editorDeadline&&!editor){
   for(const frame of context.pages().flatMap(p=>p.frames())){
    if(await frame.locator(`body#tinymce .plugin-title[data-guid="${request.fixtureId}"]`).count()===1){editor=frame;break;}
   }
   if(!editor)await page.waitForTimeout(400);
  }
  if(!editor){
   for(const frame of context.pages().flatMap(p=>p.frames()))diagnostics.push({
    editors:await frame.locator('body#tinymce').count(),
    identifiedTitles:await frame.locator('body#tinymce .plugin-title[data-guid]').count(),
    expectedIdentity:await frame.locator(`body#tinymce .plugin-title[data-guid="${request.fixtureId}"]`).count()});
   throw Error('editor_identity_missing');
  }
  await page.waitForTimeout(2000);
  const result=await editor.locator('body#tinymce').evaluate(async(body,scope)=>{
   const imgs=[...body.querySelectorAll('img')];
   await Promise.all(imgs.map(img=>img.decode().catch(()=>{})));
   const samples={};
   for(const label of ['加粗','斜体','下划线','删除线','高亮','组合样式 😀','链接文字','已完成验收项','未完成验收项','第一项','第二项']){
    samples[label]=[];
    const walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);let node;
    while((node=walker.nextNode())){
     if(node.textContent.trim()!==label)continue;
     const sample={bold:false,italic:false,underline:false,explicitUnderline:false,strike:false,highlight:false,expectedLink:false,ancestors:[]};
     for(let el=node.parentElement,depth=0;el&&el!==body&&depth<7;el=el.parentElement,depth++){
      const css=getComputedStyle(el);
      sample.bold ||= Number.parseInt(css.fontWeight,10)>=600;
      sample.italic ||= css.fontStyle==='italic';
      sample.underline ||= css.textDecorationLine.includes('underline');
      sample.explicitUnderline ||= el.tagName==='U'||el.style.textDecorationLine.includes('underline')||el.style.textDecoration.includes('underline');
      sample.strike ||= css.textDecorationLine.includes('line-through');
      sample.highlight ||= (el.tagName==='MARK'||(el.tagName==='SPAN'&&!!el.style.backgroundColor))
       &&!['transparent','rgba(0, 0, 0, 0)','rgb(255, 255, 255)'].includes(css.backgroundColor);
      sample.expectedLink ||= el.tagName==='A'&&el.getAttribute('href')==='https://example.com/note-bridge';
      sample.ancestors.push({tag:el.tagName,classes:[...el.classList],attributes:Object.fromEntries(
       ['done','data-done','data-checked','aria-checked','role'].filter(k=>el.hasAttribute(k)).map(k=>[k,el.getAttribute(k)]))});
     }
     samples[label].push(sample);
    }
   }
   return {titleMatches:body.querySelector('.plugin-title h1')?.textContent.trim()===scope.title,
    images:imgs.map(img=>({decoded:img.naturalWidth>0&&img.naturalHeight>0,visible:img.getBoundingClientRect().width>0,
     ratio:img.naturalHeight?img.naturalWidth/img.naturalHeight:0})),
    bodyCharacters:body.innerText.length,bodyTextMatches:body.innerText.replace(/\s/g,'').includes(scope.expectedText.replace(/\s/g,'')),textComparison:'full ordered text excluding whitespace',samples};
  },request);
  result.structures=await editor.locator('body#tinymce').evaluate((body,scope)=>{
   const visible=el=>el.getBoundingClientRect().width>0&&el.getBoundingClientRect().height>0;
   const headings=(scope.headings||[]).map(item=>({level:item.level,
    matched:[...body.querySelectorAll(`h${item.level}`)].some(el=>el.textContent.trim()===item.text.trim()&&visible(el))}));
   const quotes=(scope.quotes||[]).map(text=>[...body.querySelectorAll('blockquote')].some(el=>el.textContent.trim()===text.trim()&&visible(el)));
   const dividers=[...body.querySelectorAll('hr')].filter(visible).length;
   const tables=[...body.querySelectorAll('table')].filter(visible).map(table=>[...table.rows].map(row=>[...row.cells].map(cell=>({text:cell.innerText.trim(),rowSpan:cell.rowSpan,colSpan:cell.colSpan,visible:visible(cell)}))));
   const tableMatches=(scope.tables||[]).map(expected=>tables.some(rows=>rows.length===expected.length&&rows.every((row,i)=>row.length===expected[i].length&&row.every((cell,j)=>cell.visible&&cell.rowSpan===1&&cell.colSpan===1&&cell.text===expected[i][j]))));
   return {headings,quotes,dividers,expectedDividers:scope.dividers||0,tableMatches,tableCount:tables.length,expectedTableCount:(scope.tables||[]).length};
  },request);
  const tablesVerified=result.structures.tableCount===result.structures.expectedTableCount&&result.structures.tableMatches.every(Boolean);
  const verified=tablesVerified&&result.titleMatches&&result.bodyTextMatches&&(!request.group_name||groupSelected)&&result.images.length===request.expected_images&&result.images.every(i=>i.decoded&&i.visible&&Math.abs(i.ratio-640/240)<0.03);
  console.log(JSON.stringify({kind:'vivo-matrix-native',formal_acceptance:false,cloud_writes:0,
   status:verified?'images_title_verified':'needs_review',identity_matched:true,groupSelected,...result,network,blocked}));
 }catch(error){console.log(JSON.stringify({kind:'vivo-matrix-native',formal_acceptance:false,cloud_writes:0,status:'blocked',stage,code:error.message==='scope'?'scope':error.name,diagnostics,network,blocked}));}
 finally{if(browser)await browser.close();}
});
