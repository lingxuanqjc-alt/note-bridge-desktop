/* Read-only inspection of known official controls. No note text or credentials leave this process. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,stage='starting';
 try{
  const request=JSON.parse(input);input='';
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  const page=await context.newPage();stage='navigate';
  await page.goto('https://cloud.huawei.com/home',{waitUntil:'domcontentloaded',timeout:30000});
  stage='open_memo';
  const entry=page.getByText('备忘录',{exact:true}).first();
  await entry.waitFor({state:'visible',timeout:20000});await entry.click();
  // The official notepad route loads its controller asynchronously after the home tile click.
  let editorLoaded=false;
  const deadline=Date.now()+25000;
  while(Date.now()<deadline&&!editorLoaded){
   for(const candidate of context.pages())for(const frame of candidate.frames()){
    if(await frame.locator('.create-icon').first().isVisible().catch(()=>false))editorLoaded=true;
   }
   if(!editorLoaded)await page.waitForTimeout(300);
  }
  const report={kind:'official-huawei-page-contract',formal_acceptance:false,cloud_writes:0,frames:[]};
  for(const candidate of context.pages())for(const frame of candidate.frames()){
   const labels={};
   for(const label of ['新建','新建备忘录','新增','全部备忘录','所有备忘录','登录','笔记互迁验收 · 华为双图片 H3']){
    labels[label]=await frame.getByText(label,{exact:true}).count().catch(()=>0);
   }
   report.frames.push({host:(()=>{try{return new URL(frame.url()).hostname}catch{return 'local'}})(),labels,
    editable_count:await frame.locator('[contenteditable=true],textarea').count().catch(()=>0),
    file_input_count:await frame.locator('input[type=file]').count().catch(()=>0),
    editor_controls:await frame.locator('[contenteditable=true],textarea,input[type=file]').evaluateAll(nodes=>nodes.map(n=>({tag:n.tagName,
     classes:[...n.classList].filter(c=>/^[a-zA-Z][a-zA-Z_-]{0,50}$/.test(c)),visible:!!(n.offsetWidth||n.offsetHeight),
     type:n.getAttribute('type'),accept:n.getAttribute('accept')}))).catch(()=>[])});
  }
  report.editor_loaded=editorLoaded;
  report.status=editorLoaded?'inspected':'blocked_before_editor';console.log(JSON.stringify(report));
 }catch(error){console.log(JSON.stringify({kind:'official-huawei-page-contract',status:'blocked',stage,code:error.name}))}
 finally{if(browser)await browser.close()}
});
