/* Read-only native rendering check, restricted to a verified synthetic image note. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,stage='starting';
 try{
  const request=JSON.parse(input);input='';
  if(!['笔记互迁验收 · 华为双图片 H5','笔记互迁验收 · 华为分组 K2'].includes(request.title))throw Error('fixture_scope');
  if(request.group_name&&request.group_name!=='笔记互迁分组验收 K')throw Error('group_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  await context.route('**/notepad/**',route=>{
   const endpoint=new URL(route.request().url()).pathname;
   return ['/notepad/note/query','/notepad/notetag/query','/notepad/simplenote/query','/notepad/sync'].includes(endpoint)?route.continue():route.abort();
  });
  const page=await context.newPage();stage='navigate';
  await page.goto('https://cloud.huawei.com/home',{waitUntil:'domcontentloaded',timeout:30000});
  const entry=page.getByText('备忘录',{exact:true}).first();await entry.waitFor({state:'visible',timeout:20000});await entry.click();
  let groupSelected=false;
  if(request.group_name){
   stage='select_group';
   const group=page.locator('.category_item_name:visible').filter({hasText:request.group_name});
   await group.waitFor({state:'visible',timeout:25000});await group.click();groupSelected=true;
  }
  stage='select_fixture';
  const target=page.getByText(request.title,{exact:false}).first();
  await target.waitFor({state:'visible',timeout:25000});await target.click();
  await page.waitForFunction(title=>document.querySelector('#note_title_editor .CodeMirror')?.CodeMirror?.getValue()===title,request.title,{timeout:10000});
  stage='decode_images';
  const images=page.locator('#note_detail_editor .editor_image img');
  await images.first().waitFor({state:'visible',timeout:15000});
  for(let index=0;index<await images.count();index++)await images.nth(index).scrollIntoViewIfNeeded();
  // The editor loads images asynchronously and may defer those below the viewport.
  await page.waitForFunction(()=>{
   const nodes=[...document.querySelectorAll('#note_detail_editor .editor_image img')];
   return nodes.length===2&&nodes.every(n=>n.complete&&n.naturalWidth>0&&n.naturalHeight>0);
  },null,{timeout:20000}).catch(()=>{});
  const sizes=await images.evaluateAll(async nodes=>{
   await Promise.allSettled(nodes.map(n=>n.decode()));
   return nodes.map(n=>({width:n.naturalWidth,height:n.naturalHeight}));
  });
  const report={kind:'official-huawei-fixture-browser',formal_acceptance:false,images:sizes.length,
   decoded:sizes.filter(s=>s.width>0&&s.height>0).length,preview_sizes:sizes,group_selected:groupSelected,
   aspect_ratios_match:sizes.every(s=>Math.abs(s.width/s.height-640/240)<0.03)};
  report.status=report.images===2&&report.decoded===2&&report.aspect_ratios_match?'verified':'needs_review';
  try{await images.first().screenshot({path:path.resolve(__dirname,'../.private/evidence/huawei-fixture-browser.png')});report.screenshot_captured=true}catch{report.screenshot_captured=false}
  console.log(JSON.stringify(report));
 }catch(error){console.log(JSON.stringify({kind:'official-huawei-fixture-browser',status:'blocked',stage,code:error.name}))}
 finally{if(browser)await browser.close()}
});
