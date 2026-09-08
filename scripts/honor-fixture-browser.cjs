/* Read only: open the independently created synthetic note in the official renderer. */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',part=>input+=part);
process.stdin.on('end',async()=>{
 let browser,stage='starting';
 try{
  const request=JSON.parse(input);input='';
  if(!request.title.startsWith('笔记互迁验收 · 荣耀双图片 ')&&!['笔记互迁验收 · 荣耀分组 N1','笔记互迁验收 · 荣耀分组 N2','笔记互迁验收 · 荣耀富文本 S1','笔记互迁验收 · 荣耀富文本 S2'].includes(request.title))throw Error('fixture_scope');
  if(request.group_name&&request.group_name!=='笔记互迁分组验收 N')throw Error('group_scope');
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({viewport:{width:1200,height:850},locale:'zh-CN'});
  await context.addCookies(request.cookies);request.cookies.length=0;
  const page=await context.newPage();stage='navigate';
  await page.goto('https://cloud.honor.com/portal/notepad',{waitUntil:'domcontentloaded',timeout:30000});
  let groupSelected=false;
  if(request.group_name){
   stage='select_group';
   const group=page.getByText(request.group_name,{exact:true}).first();
   await group.waitFor({state:'visible',timeout:20000});await group.click();groupSelected=true;
  }
  stage='select_fixture';
  const target=page.getByText(request.title,{exact:true}).first();
  await target.waitFor({state:'visible',timeout:20000});await target.click();
  stage='decode_images';
  const images=page.locator('.Ni[hid] img');
  await images.first().waitFor({state:'visible',timeout:15000});
  const sizes=await images.evaluateAll(async nodes=>{
   await Promise.all(nodes.map(node=>node.decode()));
   return nodes.map(node=>({width:node.naturalWidth,height:node.naturalHeight}));
  });
  const result={kind:'official-honor-fixture-browser',formal_acceptance:false,images:sizes.length,
   decoded:sizes.filter(s=>s.width>0&&s.height>0).length,preview_sizes:sizes,group_selected:groupSelected};
  result.aspect_ratios_match=sizes.every(s=>Math.abs(s.width/s.height-640/240)<0.03);
  result.status=result.images===2&&result.decoded===2&&result.aspect_ratios_match?'verified':'needs_review';
  if(['笔记互迁验收 · 荣耀富文本 S1','笔记互迁验收 · 荣耀富文本 S2'].includes(request.title)){
   result.rich_styles=await page.evaluate(expectedText=>{
    const node=[...document.querySelectorAll('h-text font')].find(node=>node.textContent==='组合样式 😀');
    const style=node?getComputedStyle(node):null;
    return {combined_span_found:!!node,bold:!!style&&Number(style.fontWeight)>=600,
     italic:style?.fontStyle==='italic',underline:style?.textDecorationLine.includes('underline')||
      (!!node?.classList.contains('Lc')&&parseFloat(style?.borderBottomWidth)>0&&style?.borderBottomStyle==='solid'&&
       style?.borderBottomColor!=='transparent'&&style?.borderBottomColor!=='rgba(0, 0, 0, 0)'),
     strike:style?.textDecorationLine.includes('line-through')||false,
     highlight:!!style&&style.backgroundColor!=='rgba(0, 0, 0, 0)'&&style.backgroundColor!=='transparent',
     link_preserved:!!document.querySelector('[hlink="https://example.com/note-bridge"]'),
     heading_preserved:[...document.querySelectorAll('[hlevel="2"]')].some(node=>node.textContent.includes('二级标题 · 富文本验收')),
     ordinary_text_preserved:[...document.querySelectorAll('h-text')].some(node=>node.textContent.includes(expectedText))};
   },request.title.endsWith('S1')?'<script>仅作为文本</script>':'普通文字 & 中文');
   if(!Object.values(result.rich_styles).every(Boolean))result.status='needs_review';
  }
  // Capture only the image wrapper belonging to the synthetic fixture, excluding the account and note list.
  await images.first().locator('xpath=ancestor::*[contains(@class,"Nk")][1]').screenshot({path:path.resolve(__dirname,'../.private/evidence/honor-fixture-browser.png')});
  console.log(JSON.stringify(result));
 }catch(error){console.log(JSON.stringify({kind:'official-honor-fixture-browser',formal_acceptance:false,status:'blocked',stage,code:error.name}))}
 finally{if(browser)await browser.close()}
});
