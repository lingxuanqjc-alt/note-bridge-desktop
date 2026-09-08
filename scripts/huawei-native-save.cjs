/* One-use, synthetic-only native save investigation. Sessions remain in memory. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const fs=require('node:fs');
const path=require('node:path');
const receiptPath=path.resolve(__dirname,'../.private/evidence/huawei-native-H4-receipt.json');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,stage='starting',createdId=null,createSent=false,updates=0,ownsReceipt=false;
 const report={kind:'huawei-native-H4',formal_acceptance:false,status:'not_started',requests:[]};
 const persist=()=>fs.writeFileSync(receiptPath,JSON.stringify({...report,remote_id:createdId},null,2));
 try{
  const request=JSON.parse(input);input='';
  const title='笔记互迁验收 · 官网保存 H4';
  if(request.title!==title||fs.existsSync(receiptPath))throw Error('one_use_scope');
  fs.writeFileSync(receiptPath,JSON.stringify(report),{flag:'wx'});ownsReceipt=true;
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  await context.route('**/notepad/**',async route=>{
   const req=route.request(),endpoint=new URL(req.url()).pathname;
   if(['/notepad/note/query','/notepad/notetag/query','/notepad/simplenote/query','/notepad/sync'].includes(endpoint))return route.continue();
   if(!['/notepad/note/create','/notepad/note/update'].includes(endpoint))return route.abort();
   let payload,body;
   try{payload=req.postDataJSON();body=JSON.parse(payload.reqInfo.data)}catch{return route.abort()}
   if(!String(body.content?.title||'').includes(title))return route.abort();
   if(endpoint.endsWith('/create')){
    if(createSent||!String(payload.guid).startsWith('newNote'))return route.abort();
    createSent=true;
   }else{
    if(!createdId||payload.reqInfo.guid!==createdId||updates>=2)return route.abort();
    updates++;
   }
   const item={operation:endpoint.endsWith('/create')?'create':'update',state:'sending',
    envelope_keys:Object.keys(payload),request_keys:Object.keys(payload.reqInfo),
    body_guid_matches_envelope:body.guid===(payload.guid||payload.reqInfo.guid),
    body_guid_is_client_id:String(body.guid).startsWith('newNote'),
    content_keys:Object.keys(body.content),has_attachment:body.content.has_attachment,
    version:body.content.version,file_count:body.fileList?.length||0};
   report.requests.push(item);report.status='needs_review';persist();
   try{
    const response=await route.fetch({maxRedirects:0,maxRetries:0,timeout:30000});
    const data=await response.json();
    item.response_success=data.Result?.code==='0';
    item.state=item.response_success?'acknowledged':'unconfirmed';
    if(item.operation==='create'&&item.response_success&&typeof data.rspInfo?.guid==='string')createdId=data.rspInfo.guid;
    persist();await route.fulfill({response});
   }catch{item.state='uncertain';persist();await route.abort()}
  });
  const page=await context.newPage();stage='navigate';
  await page.goto('https://cloud.huawei.com/home',{waitUntil:'domcontentloaded',timeout:30000});
  const entry=page.getByText('备忘录',{exact:true}).first();await entry.waitFor({state:'visible',timeout:20000});await entry.click();
  await page.locator('.create-icon').first().waitFor({state:'visible',timeout:25000});
  stage='new_fixture';await page.locator('.create-icon').first().click();
  const titleEditor=page.locator('#note_title_editor .CodeMirror');
  await titleEditor.waitFor({state:'visible',timeout:10000});
  // The new editor must be empty before any input; never select-all an existing note.
  await page.waitForFunction(()=>{
   const editors=[...document.querySelectorAll('#note_title_editor .CodeMirror')];
   return editors.length===1&&editors[0].CodeMirror?.getValue()==='';
  },null,{timeout:5000});
  await titleEditor.click();await page.keyboard.insertText(title);
  await page.locator('#note_detail_editor .CodeMirror').click();
  await page.keyboard.insertText('独立官网保存验证 H4，中文 English。');
  stage='wait_create';
  let deadline=Date.now()+20000;
  while(!createdId&&Date.now()<deadline)await page.waitForTimeout(300);
  if(!createdId)throw Error('create_unconfirmed');
  stage='update_fixture';await page.locator('#note_detail_editor .CodeMirror').click();
  await page.keyboard.press('Control+End');await page.keyboard.insertText(' 第二次保存验证。');
  deadline=Date.now()+20000;
  while(!report.requests.some(r=>r.operation==='update'&&r.state==='acknowledged')&&Date.now()<deadline)await page.waitForTimeout(300);
  report.status=report.requests.some(r=>r.operation==='update'&&r.state==='acknowledged')?'save_acknowledged':'needs_review';
 }catch(error){report.stage=stage;report.error=error.name;
  report.failure_flags=['strict mode violation','detached','Timeout','new_editor_not_empty','one_use_scope'].filter(flag=>String(error.message).includes(flag));
  report.status=createSent?'needs_review':'blocked_before_write'}
 finally{
  if(browser)await browser.close();
  if(ownsReceipt)persist();
  console.log(JSON.stringify(report));
 }
});
