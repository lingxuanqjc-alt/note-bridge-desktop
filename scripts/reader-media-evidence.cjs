/* Generate our own media and verify ZIP-relocated file:// playback without cloud access. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const {pathToFileURL,fileURLToPath}=require('node:url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH||'playwright');
const root=path.resolve(__dirname,'..');
const out=path.join(root,'.private/evidence','reader-media-'+Date.now());
fs.mkdirSync(path.join(out,'resources'),{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{
  const context=await browser.newContext({viewport:{width:1200,height:800}});
  const remote=[],errors=[];
  await context.route(/^https?:/,route=>{remote.push(route.request().url());return route.abort()});
  const page=await context.newPage();
  page.on('pageerror',error=>errors.push(error.name));
  const bytes=await page.evaluate(async()=>{
   const canvas=document.createElement('canvas');canvas.width=160;canvas.height=90;
   const ctx=canvas.getContext('2d'),stream=canvas.captureStream(15);
   const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8'}),chunks=[];
   recorder.ondataavailable=e=>chunks.push(e.data);
   const stopped=new Promise(resolve=>{recorder.onstop=resolve});
   recorder.start();
   for(let frame=0;frame<18;frame++){
    ctx.fillStyle=frame%2?'#7a6fe8':'#25304a';ctx.fillRect(0,0,160,90);
    await new Promise(resolve=>setTimeout(resolve,70));
   }
   recorder.stop();await stopped;stream.getTracks().forEach(track=>track.stop());
   return [...new Uint8Array(await new Blob(chunks).arrayBuffer())];
  });
  fs.writeFileSync(path.join(out,'resources/sample.webm'),Buffer.from(bytes));
  const prepared=spawnSync(path.join(root,'.venv/Scripts/python.exe'),[path.join(__dirname,'reader-media-fixture.py'),out],{encoding:'utf8',windowsHide:true,timeout:60000,env:{...process.env,PYTHONUTF8:'1'}});
  assert.equal(prepared.status,0,prepared.stderr);
  const fixture=JSON.parse(prepared.stdout),playbacks=[];
  for(const reader of fixture.readers){
   await page.goto(pathToFileURL(reader.path).href);
   const rootSelector=reader.multi?'article':'#content';
   async function playback(){
    const media=page.locator(rootSelector+' audio,'+rootSelector+' video');
    assert.equal(await media.count(),2);
    for(let i=0;i<2;i++){
     const result=await media.nth(i).evaluate(async element=>{
      element.muted=true;
      const ended=new Promise((resolve,reject)=>{
       const timer=setTimeout(()=>reject(Error('media_playback_timeout')),15000);
       element.onended=()=>{clearTimeout(timer);resolve()};
       element.onerror=()=>{clearTimeout(timer);reject(Error('media_decode_failed'))};
      });
      await element.play();await ended;
      return {type:element.tagName,time:element.currentTime,width:element.videoWidth||0,src:element.currentSrc};
     });
     assert(result.time>.5,'Playback must advance through actual samples');
     if(result.type==='VIDEO')assert.equal(result.width,160);
     assert.equal(path.dirname(fileURLToPath(result.src)),path.join(path.dirname(reader.path),'resources'));
     playbacks.push({type:result.type,time:result.time,multi:reader.multi});
    }
    await page.locator(rootSelector+' img').evaluate(image=>image.decode());
   }
   await playback();
   if(!reader.multi){
    await page.locator('#query').fill('alpha beta');
    assert.equal(await page.locator('#notes button').count(),2);
    assert.equal(await page.locator('mark.hit').count(),2);
    await page.locator('#query').press('Enter');
    assert.equal(await page.locator('#page').textContent(),'2 / 2');
    await playback();
   }
  }
  assert.deepEqual(remote,[]);assert.deepEqual(errors,[]);
  const evidence={kind:'synthetic-media-relocated-offline-reader',formal_acceptance:false,cloud_writes:0,
   exports:fixture.checks,playbacks,remote_requests:0,js_errors:0,
   scope:'Generated WAV and VP8 WebM playback to completion, image decoding, ZIP relocation, four formats/two modes, AND search and page switch; not vendor attachment coverage or all codecs.'};
  fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify({evidence:path.relative(root,path.join(out,'evidence.json')),export_checks:fixture.checks.length,playbacks:playbacks.length}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
