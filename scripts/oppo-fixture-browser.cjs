/* Read only: open the independently created synthetic note in the official renderer. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,stage='starting';
 try{
  const request=JSON.parse(input);input='';
  if(!request.title.startsWith('笔记互迁验收 · OPPO双图片 '))throw Error('fixture_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  const page=await context.newPage();stage='navigate';
  await page.goto('https://cloud.oppo.com/',{waitUntil:'domcontentloaded',timeout:30000});
  stage='select_fixture';
  let selectedFrame,entered=false;
  const deadline=Date.now()+30000;
  while(Date.now()<deadline&&!selectedFrame){
   for(const candidate of context.pages())for(const frame of candidate.frames()){
    const target=frame.getByText(request.title,{exact:false}).first();
    if(await target.isVisible().catch(()=>false)){await target.click();selectedFrame=frame;break}
    if(!entered){
     const entry=frame.getByText('笔记',{exact:true}).first();
     if(await entry.isVisible().catch(()=>false)){await entry.click();entered=true}
    }
   }
   if(!selectedFrame)await new Promise(resolve=>setTimeout(resolve,300));
  }
  if(!selectedFrame)throw Error('fixture_not_found');
  stage='locate_images';
  // Selecting a note can replace the official application's frame.
  let images;
  const imageDeadline=Date.now()+15000;
  while(Date.now()<imageDeadline&&!images){
   for(const candidate of context.pages())for(const frame of candidate.frames()){
    const current=frame.locator('.ProseMirror img');
    if(await current.first().isVisible().catch(()=>false)){
     const fixtureVisible=await frame.getByText(request.title,{exact:false}).first().isVisible().catch(()=>false);
     if(fixtureVisible){images=current;break}
    }
   }
   if(!images)await new Promise(resolve=>setTimeout(resolve,300));
  }
  if(!images)throw Error('fixture_images_not_visible');
  stage='decode_images';
  const sizes=await images.evaluateAll(async nodes=>{
   await Promise.allSettled(nodes.map(node=>node.decode()));
   return nodes.map(node=>({width:node.naturalWidth,height:node.naturalHeight,
    sourceIsBlob:(node.currentSrc||node.src).startsWith('blob:'),sourceIsData:(node.currentSrc||node.src).startsWith('data:')}));
  });
  const result={kind:'official-oppo-fixture-browser',formal_acceptance:false,images:sizes.length,
   decoded:sizes.filter(s=>s.width>0&&s.height>0).length,preview_sizes:sizes};
  result.aspect_ratios_match=sizes.every(s=>Math.abs(s.width/s.height-640/240)<0.03);
  result.status=result.images===2&&result.decoded===2&&result.aspect_ratios_match?'verified':'needs_review';
  // Capture only the image wrapper belonging to the synthetic fixture, excluding the account and note list.
  stage='capture_image';
  try{await images.first().screenshot({path:path.resolve(__dirname,'../.private/evidence/oppo-fixture-browser.png')});result.screenshot_captured=true}
  catch{result.screenshot_captured=false}
  console.log(JSON.stringify(result));
 }catch(error){console.log(JSON.stringify({kind:'official-oppo-fixture-browser',formal_acceptance:false,status:'blocked',stage,code:error.name,
  failure_flags:['detached','strict mode violation','Timeout','closed','destroyed'].filter(flag=>String(error.message).includes(flag))}))}
 finally{if(browser)await browser.close()}
});
