/* Render only the supplied static frontend in an isolated browser; never execute its EXE. */
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const JSZip=require(process.env.JSZIP_MODULE_PATH||'jszip');
const run=process.env.EVIDENCE_RUN;
assert(!run||/^[A-Z0-9-]+$/.test(run));
const root=path.resolve(__dirname,'..'),out=path.join(root,'.private/evidence',run?'reference-'+run:'reference');
const archivePath=process.argv[2];
if(!archivePath)throw Error('Pass the reference ZIP path');
const bytes=fs.readFileSync(archivePath);
assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),'6d3a8260a3875a85e3dca2e5108bd6863df96062edc4b975f880f39f9c01b31f');
fs.mkdirSync(out,{recursive:true});
const evidence={kind:'isolated-original-frontend',formal_acceptance:false,viewport:{width:1200,height:800},original_executable_run:false,external_requests_allowed:false,states:{}};
(async()=>{
 const zip=await JSZip.loadAsync(bytes);
 const entry=Object.keys(zip.files).find(name=>name.endsWith('/NoteExport.exe'));
 const executable=await zip.file(entry).async('nodebuffer');
 const text=executable.subarray(45490000,47843000).toString('latin1');
 const names=[...text.matchAll(/u((?:assets\/)[a-zA-Z0-9_./-]+\.(?:js|png|ico|woff2|ttf)|index\.html)/g)].map(m=>m[1]);
 const runs=[...text.matchAll(/[A-Za-z0-9+/]{1000,}={0,2}/g)].slice(1);
 assert.equal(names.length,19);assert.equal(runs.length,19);
  const assets=new Map(names.map((name,i)=>['/'+name,Buffer.from(runs[i][0].slice(1),'base64')]));
 const fragments=[];
 for(const [name,content] of assets){
  if(!name.endsWith('.js'))continue;
  const source=content.toString('utf8');
  for(const term of ['登录提示','确定并','登录说明','小米 笔记登录提示','小米笔记登录提示','jsx(Db,','jsx(Cb,','Db=','en=({']){
   const at=source.indexOf(term);
   if(at>=0)fragments.push({asset:name,term,context:source.slice(Math.max(0,at-1800),at+1800)});
  }
 }
 fs.writeFileSync(path.join(out,'private-layout-fragments.json'),JSON.stringify(fragments,null,2));
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 try {
  const context=await browser.newContext({viewport:evidence.viewport,deviceScaleFactor:1,locale:'zh-CN',acceptDownloads:false,reducedMotion:'reduce'});
  await context.route('**/*',route=>{
   const url=new URL(route.request().url()),name=url.pathname==='/'?'/index.html':url.pathname;
   if(url.origin!=='https://reference.invalid'||!assets.has(name))return route.abort();
   const mime={'.html':'text/html','.js':'text/javascript','.png':'image/png','.ico':'image/x-icon','.woff2':'font/woff2','.ttf':'font/ttf'}[path.extname(name)];
   return route.fulfill({body:assets.get(name),contentType:mime});
  });
  const page=await context.newPage();
  await page.goto('https://reference.invalid/');await page.getByText('小米笔记',{exact:true}).waitFor();
  await page.evaluate(()=>document.fonts.ready);
  async function settle(){await page.evaluate(()=>new Promise(resolve=>{
   let previous='',stable=0;function frame(){const r=document.querySelector('main').getBoundingClientRect();const value=[r.x,r.y,r.width,r.height].join(',');stable=value===previous?stable+1:0;previous=value;if(stable>=15)resolve();else requestAnimationFrame(frame)}frame();
  }))}
  await settle();
  async function rectangles(label){
   const found=await page.getByText(label,{exact:true}).first().evaluate(element=>{
    const results=[];for(let node=element;node&&node!==document.body;node=node.parentElement){const r=node.getBoundingClientRect();results.push({tag:node.tagName,x:r.x,y:r.y,width:r.width,height:r.height})}return results;
   });return found;
  }
  await page.screenshot({path:path.join(out,'01-initial.png')});
  const titleBoxes=await page.evaluate(()=>[...document.querySelectorAll('header,div')].map(node=>{
   const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};
  }).filter(r=>Math.abs(r.width-992)<1&&Math.abs(r.height-48)<1));
  evidence.states.initial={cardAncestors:await rectangles('小米笔记'),titleBoxes};
  async function controlMetrics(){return page.getByText('小米笔记',{exact:true}).first().evaluate(name=>{
   const card=name.closest('button'),children=[...card.querySelectorAll('*')];
   const logo=children.find(node=>{const r=node.getBoundingClientRect();return r.width===48&&r.height===48});
   const login=children.find(node=>node.tagName==='BUTTON');
   return Object.fromEntries(Object.entries({card,name,logo,login}).map(([key,node])=>{
    const r=node.getBoundingClientRect(),s=getComputedStyle(node);return[key,{x:r.x,y:r.y,width:r.width,height:r.height,fontSize:s.fontSize,fontWeight:s.fontWeight,lineHeight:s.lineHeight,color:s.color,borderRadius:s.borderRadius}];
   }));
  })}
  evidence.states.initial.controls=await controlMetrics();
  async function allCards(width,height){return page.evaluate(({width,height})=>[...document.querySelectorAll('button')]
   .filter(card=>{const r=card.getBoundingClientRect();return r.width===width&&r.height===height})
   .map(card=>{
    const children=[...card.querySelectorAll('*')],login=children.find(n=>n.tagName==='BUTTON');
    const logo=children.find(n=>{const r=n.getBoundingClientRect();return r.width===48&&r.height===48});
    const labels=['小米笔记','OPPO笔记','vivo笔记','华为备忘录','荣耀笔记','魅族笔记','WPS便签'];
    const name=children.find(n=>labels.includes(n.textContent.trim())&&n.children.length===0);
    return Object.fromEntries(Object.entries({card,logo,name,login}).map(([key,node])=>{
     const r=node.getBoundingClientRect();return[key,{x:r.x,y:r.y,width:r.width,height:r.height}];
    }));
   }),{width,height})}
  evidence.states.initial.cards=await allCards(240,94);
  await page.getByText('小米笔记',{exact:true}).click();
  await page.screenshot({path:path.join(out,'02-selected.png')});
  await page.getByRole('button',{name:'登录',exact:true}).first().click();
  await page.waitForTimeout(500);
  await page.screenshot({path:path.join(out,'03-login-attempt.png')});
  evidence.states.loginAttempt=await page.evaluate(()=>{
   const box=node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};
   return {text:document.body.innerText,commercialGate:document.body.innerText.includes('会员中心'),panels:[...document.querySelectorAll('div')].map(box).filter(r=>r.width>=300&&r.width<=750&&r.height>=150&&r.height<=650&&Math.abs(r.x+r.width/2-innerWidth/2)<3&&Math.abs(r.y+r.height/2-innerHeight/2)<3)};
  });
  evidence.login_dialog_observed=!evidence.states.loginAttempt.commercialGate&&evidence.states.loginAttempt.text.includes('小米笔记登录提示');
  await page.goto('https://reference.invalid/');
  await page.getByText('小米笔记',{exact:true}).waitFor();
  await page.getByRole('button',{name:/迁移笔记/}).first().click();
  await page.getByText('小米笔记',{exact:true}).nth(1).waitFor();
  await settle();
  evidence.states.migration={cardAncestors:await rectangles('小米笔记'),controls:await controlMetrics()};
  evidence.states.migration.cards=await allCards(300,70);
  await page.screenshot({path:path.join(out,'08-migration.png')});
  fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence));
 } finally {await browser.close()}
})().catch(error=>{console.error(error.name+': '+error.message);process.exitCode=1});
