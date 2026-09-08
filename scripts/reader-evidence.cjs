/* Public synthetic content and private aggregate-only browser checks. No cloud access. */
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const [fixture, live, output] = process.argv.slice(2);
const evidence = {kind:'offline-reader-browser',formal_acceptance:false,checks:[]};
(async()=>{
  const browser = await chromium.launch({executablePath:process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1200,height:800}});
    const remote = [], errors = [];
    await context.route(/^https?:/, route=>{remote.push(new URL(route.request().url()).hostname);return route.abort()});
    const page = await context.newPage();
    page.on('pageerror', error=>errors.push(error.name));
    await page.goto(pathToFileURL(fixture).href);
    assert.equal(await page.locator('#notes button').count(),4);
    await page.locator('#query').fill('alpha beta');
    assert.equal(await page.locator('#notes button').count(),2,'Every search word must match the same note');
    assert.equal(await page.locator('mark.hit').count(),3);
    await page.locator('#query').press('Enter');
    assert.equal(await page.locator('#page').textContent(),'2 / 2');
    await page.locator('#query').press('Shift+Enter');
    assert.equal(await page.locator('#page').textContent(),'1 / 2');
    await page.locator('#month').fill('2026-09');
    assert.equal(await page.locator('#notes button').count(),1);
    assert.equal(await page.locator('#content h2').textContent(),'Third');
    await page.locator('#query').fill('absent');
    assert.equal(await page.locator('#page').textContent(),'0 / 0');
    assert.equal(await page.locator('#slider').isDisabled(),true);
    await page.locator('#month').fill('');
    await page.locator('#query').fill('');
    await page.locator('#slider').evaluate(slider=>{slider.value='3';slider.dispatchEvent(new Event('input'))});
    assert.equal(await page.locator('#page').textContent(),'4 / 4');
    assert.equal(await page.evaluate(()=>window.noteExecuted === undefined),true);
    assert.equal(await page.locator('#content script,#content [onclick],#content [onerror],#content a[href^="javascript:"]').count(),0);
    evidence.checks.push('AND search','case-insensitive search','highlighting','Enter and Shift+Enter navigation',
      'creation-month filter','empty results','slider navigation','executable note content removed');
    await page.goto(pathToFileURL(live).href);
    const count = await page.locator('#notes button').count();
    const mediaIndexes = await page.evaluate(()=>JSON.parse(document.getElementById('note-data').textContent)
      .map((n,i)=> /<(img|audio|video)\b/.test(n.html) ? i : -1).filter(i=>i>=0));
    let images = 0, audio = 0, videos = 0;
    for(const index of mediaIndexes) {
      await page.locator('#slider').evaluate((slider,value)=>{slider.value=String(value);slider.dispatchEvent(new Event('input'))},index);
      const img = page.locator('#content img');
      for(let i=0; i<await img.count();i++) {
        await img.nth(i).scrollIntoViewIfNeeded();
        await img.nth(i).evaluate(image=>image.decode());
        assert.equal(await img.nth(i).evaluate(image=>image.naturalWidth>0),true);
        images++;
      }
      const media = page.locator('#content audio,#content video');
      for(let i=0;i<await media.count();i++) {
        const type = await media.nth(i).evaluate(async element=>{
          await new Promise((resolve,reject)=>{
            const timer=setTimeout(()=>reject(new Error('media_metadata_timeout')),15000);
            element.onloadedmetadata=()=>{clearTimeout(timer);resolve()};
            element.onerror=()=>{clearTimeout(timer);reject(new Error('media_decode_failed'))};
            element.preload='metadata';element.load();
          });
          if(!(element.duration>0))throw new Error('media_duration');
          return element.tagName;
        });
        if(type==='AUDIO')audio++;else videos++;
      }
    }
    assert.ok(images>0,'The real reader must actually decode downloaded images');
    assert.deepEqual(remote,[],'Offline reading must never request a vendor or external host');
    assert.deepEqual(errors,[]);
    evidence.live={notes:count,media_notes:mediaIndexes.length,images,audio,videos,remote_requests:0,js_errors:0};
    evidence.checks.push('real local image decoding','no external requests');
    if(audio>0)evidence.checks.push('real local audio metadata');
    if(videos>0)evidence.checks.push('real local video metadata');
    evidence.uncovered_media_types=[...(!audio?['audio']:[]),...(!videos?['video']:[])];
    fs.writeFileSync(output,JSON.stringify(evidence,null,2));
    console.log(JSON.stringify(evidence));
  } finally {await browser.close()}
})().catch(error=>{console.error(JSON.stringify({status:'failed',code:error.name,message: error.message?.split('\n')[0]}));process.exitCode=1});
