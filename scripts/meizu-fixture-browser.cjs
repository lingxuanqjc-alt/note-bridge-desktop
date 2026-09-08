/* Read only: open the independently created synthetic note in the official renderer. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,currentPage,stage='starting',groupSelected=false;
 const reads=[];
 try{
  const request=JSON.parse(input);input='';
  if(!request.title.startsWith('笔记互迁验收 · 魅族双图片 ')&&!['笔记互迁验收 · 魅族分组 L1','笔记互迁验收 · 魅族分组 L2'].includes(request.title))throw Error('fixture_scope');
  if(request.group_name&&request.group_name!=='笔记互迁分组验收 L')throw Error('group_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  await context.route('**/c/browser/note/**',route=>{
   const endpoint=new URL(route.request().url()).pathname.split('/').pop();
   return ['gettags','getnotegroups','getnotebycontent'].includes(endpoint)?route.continue():route.abort();
  });
  const page=await context.newPage();currentPage=page;stage='navigate';
  page.on('response',async response=>{
   const endpoint=new URL(response.url()).pathname.split('/').pop();
   if(['gettags','getnotegroups'].includes(endpoint)){
    try{const data=await response.json();reads.push({endpoint,status:response.status(),code:data.returnCode,
     count:Array.isArray(data.returnValue?.data)?data.returnValue.data.length:data.returnValue?.count})}catch{}
   }
  });
  await page.goto('https://cloud.flyme.cn/browser/main.jsp',{waitUntil:'domcontentloaded',timeout:30000});
  stage='select_fixture';
  let selectedFrame,entered=false;
  const deadline=Date.now()+30000;
  while(Date.now()<deadline&&!selectedFrame){
   for(const candidate of context.pages())for(const frame of candidate.frames()){
    if(request.group_name&&!groupSelected){
     const group=frame.getByText(request.group_name,{exact:true}).first();
     if(await group.isVisible().catch(()=>false)){await group.click();groupSelected=true;entered=true;continue}
    }
    const target=frame.getByText(request.title,{exact:false}).first();
    if((!request.group_name||groupSelected)&&await target.isVisible().catch(()=>false)){await target.click();selectedFrame=frame;break}
    if(!entered){
     const entry=frame.getByText('笔记',{exact:true}).first();
     if(await entry.isVisible().catch(()=>false)){await entry.click();entered=true}
    }
   }
   if(!selectedFrame)await new Promise(resolve=>setTimeout(resolve,300));
  }
  if(!selectedFrame)throw Error('fixture_not_found');
  stage='decode_images';
  const images=selectedFrame.locator('.editor-content .ProseMirror img');
  await images.first().waitFor({state:'visible',timeout:15000});
  const sizes=await images.evaluateAll(async nodes=>{
   await Promise.all(nodes.map(node=>node.decode()));
   return nodes.map(node=>({width:node.naturalWidth,height:node.naturalHeight}));
  });
  const result={kind:'official-meizu-fixture-browser',formal_acceptance:false,images:sizes.length,
   decoded:sizes.filter(s=>s.width>0&&s.height>0).length,preview_sizes:sizes,group_selected:groupSelected};
  result.aspect_ratios_match=sizes.every(s=>Math.abs(s.width/s.height-640/240)<0.03);
  result.status=result.images===2&&result.decoded===2&&result.aspect_ratios_match?'verified':'needs_review';
  // Capture only the image wrapper belonging to the synthetic fixture, excluding the account and note list.
  await images.first().screenshot({path:path.resolve(__dirname,'../.private/evidence/meizu-fixture-browser.png')});
  console.log(JSON.stringify(result));
 }catch(error){
  const result={kind:'official-meizu-fixture-browser',formal_acceptance:false,status:'blocked',stage,code:error.name,group_selected:groupSelected,reads};
  if(currentPage){
   result.host=new URL(currentPage.url()).hostname;
   result.known_labels={};
   for(const label of ['登录','笔记','全部笔记','我的笔记','魅族双图片'])result.known_labels[label]=await currentPage.getByText(label,{exact:true}).count();
   result.frames=currentPage.frames().map(frame=>{try{return new URL(frame.url()).hostname}catch{return 'local'}});
  }
  console.log(JSON.stringify(result))
 }
 finally{if(browser)await browser.close()}
});
