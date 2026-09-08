/* Read only: open the independently created synthetic note in the official renderer. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,page,stage='starting';
 try{
  const request=JSON.parse(input);input='';
  const honorDirection=request.title==='笔记互迁验收 · 荣耀分组 N2';
  if(!/^笔记互迁验收 · WPS(双图片|长正文|分组) /.test(request.title)&&!honorDirection)throw Error('fixture_scope');
  if(request.group_name&&request.group_name!==(honorDirection?'笔记互迁分组验收 N':'笔记互迁分组验收 J'))throw Error('group_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  page=await context.newPage();stage='navigate';
  await page.goto('https://note.wps.cn/',{waitUntil:'domcontentloaded',timeout:30000});
  const dismiss=page.locator('.migration-dialog:not(.migration-copy-dialog) .migration-dialog__secondary-btn');
  if(request.group_name){
   stage='select_group';
   // The same name also exists in the group's rename control; select the navigation label only.
   const group=page.locator('.group_item_title:visible').filter({hasText:request.group_name});
   await group.waitFor({state:'visible',timeout:25000});
   if(await dismiss.isVisible().catch(()=>false))await dismiss.click();
   await group.click();
  }
  stage='select_fixture';
  // The vendor list shortens the summary prefix; the unique fixture suffix remains visible.
  const target=page.getByText(request.title.split(' · ')[1],{exact:false});
  await target.waitFor({state:'visible',timeout:35000});
  if(await target.count()!==1)throw Error('ambiguous_fixture');
  // The observed secondary action only dismisses the vendor announcement locally.
  // Its primary action starts an account migration and must never be used by this check.
  if(await dismiss.isVisible().catch(()=>false))await dismiss.click();
  stage='click_fixture';
  await target.click();
  stage='decode_images';
  const images=page.locator('.ql-editor img.e-img');
  await images.first().waitFor({state:'visible',timeout:15000});
  const sizes=await images.evaluateAll(async nodes=>{
   await Promise.all(nodes.map(node=>node.decode()));
   return nodes.map(node=>({width:node.naturalWidth,height:node.naturalHeight}));
  });
  const result={kind:'official-wps-fixture-browser',formal_acceptance:false,group_selected:!!request.group_name,images:sizes.length,
   decoded:sizes.filter(s=>s.width>0&&s.height>0).length,preview_sizes:sizes};
  result.aspect_ratios_match=sizes.every(s=>Math.abs(s.width/s.height-640/240)<0.03);
  result.status=result.images===2&&result.decoded===2&&result.aspect_ratios_match?'verified':'needs_review';
  // Capture only the image wrapper belonging to the synthetic fixture, excluding the account and note list.
  await images.first().screenshot({path:path.resolve(__dirname,'../.private/evidence/wps-fixture-browser.png')});
  console.log(JSON.stringify(result));
 }catch(error){
  const diagnostics=[];
  for(const frame of page?.frames()||[]){
   const labels={};
   for(const label of ['登录','立即登录','便签','新建','全部便签','同意','WPS长正文 G2']){
    labels[label]=await frame.getByText(label,{exact:false}).count().catch(()=>0);
   }
   const fixture=frame.getByText('WPS长正文 G2',{exact:false}).first();
   const ancestors=await fixture.evaluate(node=>{
    const result=[];
    for(let i=0;node&&i<6;i++,node=node.parentElement){
     const box=node.getBoundingClientRect(),style=getComputedStyle(node);
     result.push({tag:node.tagName,class:node.className,x:box.x,y:box.y,width:box.width,height:box.height,display:style.display,visibility:style.visibility});
    }
    return result;
   },{timeout:1000}).catch(()=>[]);
   diagnostics.push({host:new URL(frame.url()||'about:blank').hostname,labels,ancestors});
  }
  const failureFlags=['intercepts pointer events','not visible','not enabled','strict mode violation'].filter(flag=>String(error.message).includes(flag));
  const blockingClasses=[...String(error.message).matchAll(/class="([A-Za-z0-9_ -]{1,150})"/g)].map(match=>match[1]);
  console.log(JSON.stringify({kind:'official-wps-fixture-browser',formal_acceptance:false,status:'blocked',stage,code:error.name,failureFlags,blockingClasses:[...new Set(blockingClasses)],diagnostics}))
 }
 finally{if(browser)await browser.close()}
});
