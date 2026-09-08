/* One read-only official-page check; the scoped live session arrives only through stdin. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,currentPage,stage='starting';
 try{
  const request=JSON.parse(input);input='';
  if(!request.title.startsWith('笔记互迁验收 · vivo 图片 · ')&&!['笔记互迁验收 · vivo分组 M1','笔记互迁验收 · vivo分组 M2','笔记互迁验收 · vivo表格 T1'].includes(request.title))throw Error('invalid_fixture_scope');
  if(request.group_name&&request.group_name!=='笔记互迁分组验收 M')throw Error('group_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  const page=await context.newPage();
  currentPage=page;stage='navigate';
  await page.goto('https://pc.vivo.com.cn/suite?origin=cloudWeb',{waitUntil:'domcontentloaded',timeout:30000});
  const result={kind:'official-vivo-fixture-browser',formal_acceptance:false,status:'page_pending'};
  const welcome=page.getByText('知道了',{exact:true});
  await welcome.waitFor({state:'visible',timeout:10000}).catch(()=>{});
  if(await welcome.count()===1&&await welcome.isVisible())await welcome.click();
  await page.getByText('原子笔记',{exact:true}).first().waitFor({state:'visible',timeout:20000}).catch(()=>{});
  for(const frame of page.frames()){
   const entry=frame.getByText('原子笔记',{exact:true});
   if(await entry.count()>0&&await entry.first().isVisible()){stage='open_notes';await entry.first().click()}
  }
  let target=null,groupSelected=false;
  const deadline=Date.now()+25000;
  while(Date.now()<deadline&&!target){
   for(const frame of context.pages().flatMap(current=>current.frames())){
    if(request.group_name&&!groupSelected){
     const group=frame.getByText(request.group_name,{exact:true}).first();
     if(await group.isVisible().catch(()=>false)){await group.click();groupSelected=true;continue}
    }
    const match=frame.getByText(request.title,{exact:true});
    if((!request.group_name||groupSelected)&&await match.count()>0&&await match.first().isVisible()){target=match.first();break}
   }
   if(!target)await page.waitForTimeout(500);
  }
  if(target){
   stage='select_fixture';
   await target.click();
   const frame=await target.elementHandle().then(element=>element.ownerFrame());
   currentPage=frame.page();stage='read_images';
   // The title is outside TinyMCE; actual note images live in its nested editing iframe.
   let contentFrame;
   const editorDeadline=Date.now()+15000;
   while(Date.now()<editorDeadline&&!contentFrame){
    for(const candidate of currentPage.frames()){
     if(await candidate.locator('body#tinymce').count()){contentFrame=candidate;break}
    }
    if(!contentFrame)await page.waitForTimeout(300);
   }
   if(!contentFrame)throw Error('editor_frame_unavailable');
   const imageDeadline=Date.now()+15000;
   while(Date.now()<imageDeadline){
    const count=await contentFrame.locator('img').count();
    if(count===request.expected_images)break;
    await page.waitForTimeout(300);
   }
   const rendered=await contentFrame.locator('img').evaluateAll(async images=>{
    await Promise.all(images.map(image=>image.decode()));
    return images.map(image=>({width:image.naturalWidth,height:image.naturalHeight}));
   });
   result.images=rendered.length;
   result.group_selected=groupSelected;
   result.decoded=rendered.filter(size=>size.width>0&&size.height>0).length;
   result.preview_sizes=rendered;
   // The vendor may display its resized thumbnail. Original bytes are verified by the API readback separately.
   result.aspect_ratios_match=rendered.every(size=>Math.abs(size.width/size.height-640/240)<0.03);
   await contentFrame.locator('img').first().screenshot({path:path.resolve(__dirname,'../.private/evidence/vivo-fixture-browser.png')});
   result.status=result.images===request.expected_images&&result.decoded===request.expected_images&&result.aspect_ratios_match?'verified':'needs_review';
   if(request.title==='笔记互迁验收 · vivo表格 T1'){
    const tables=contentFrame.locator('body#tinymce table');
    const expected=[['列A','列B','列C'],['中文😀','','A | B'],['','数字 42','& < >']];
    const rows=await tables.evaluateAll(items=>items.map(table=>Array.from(table.rows,row=>Array.from(row.cells,cell=>cell.innerText.trim()))));
    result.table_count=rows.length;
    result.table_cells_match=JSON.stringify(rows)===JSON.stringify([expected]);
    result.table_visible=await tables.first().isVisible().catch(()=>false);
    if(!result.table_cells_match||!result.table_visible)result.status='needs_review';
    if(result.table_visible)await tables.first().screenshot({path:path.resolve(__dirname,'../.private/evidence/vivo-table-T1-browser.png')});
   }
  }else{
   result.frames=context.pages().flatMap(current=>current.frames()).map(frame=>{try{return new URL(frame.url()).hostname}catch{return 'local'}});
   result.search_controls=[];
   for(const frame of page.frames())result.search_controls.push(await frame.getByPlaceholder(/搜索/).count());
   result.known_labels={};
   for(const label of ['登录','笔记','我的笔记','全部笔记'])result.known_labels[label]=await page.getByText(label,{exact:true}).count();
  }
  console.log(JSON.stringify(result));
 }catch(error){
  console.log(JSON.stringify({kind:'official-vivo-fixture-browser',formal_acceptance:false,status:'blocked',stage,code:error.name}))
 }
 finally{if(browser)await browser.close()}
});
