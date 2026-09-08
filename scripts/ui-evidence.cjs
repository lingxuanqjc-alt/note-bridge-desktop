/* Isolated UI tests only. Synthetic native responses never ship in the application. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const run=process.env.EVIDENCE_RUN;
assert(!run||/^[A-Z0-9-]+$/.test(run));
const out = path.join(root, '.private/evidence',run?'ui-'+run:'ui');
fs.mkdirSync(out, {recursive:true});
const mime = {'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'};
const server = http.createServer((req,res)=>{
  const file=path.resolve(root,'ui/dist','.'+(req.url==='/'?'/index.html':req.url.split('?')[0]));
  if(!file.startsWith(path.join(root,'ui/dist')+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');
  res.end(fs.readFileSync(file));
});
const evidence = {kind:'synthetic-ui-only',formal_acceptance:false,viewport:{width:1200,height:800},checks:[],screenshots:[],states:{}};
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try{
    const context=await browser.newContext({viewport:evidence.viewport,deviceScaleFactor:1,locale:'zh-CN',reducedMotion:'reduce'});
    await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    await context.addInitScript(()=>{
      const platforms=['xiaomi','oppo','vivo','huawei','honor','meizu','wps'];
      const state={name:'笔记互迁',version:'1.0.0',release_channel:'stable',platforms:platforms.map(id=>({id,logged_in:false,logging_in:false,login_open:false,count:0,complete:false,capability:'pending'})),current_task:null,has_export:false,release_ready:false};
      const calls=[];
      window.__test={state,calls,setTask(task){state.current_task=task;window.onNoteBridgeTask?.(task)}};
      const task=(operation)=>({id:'synthetic-task',operation,status:'running',stage:'正在处理测试样例',completed:1,total:3,succeeded:1,skipped:0,issues:[],started_at:'2026-09-05T00:00:00Z'});
      const handlers={
        get_app_state:()=>state,
        login_platform:id=>{state.platforms.find(p=>p.id===id).login_open=true;return {started:true}},
        complete_login:id=>{state.platforms.find(p=>p.id===id).logged_in=true;return {logged_in:true}},
        cancel_login:id=>{state.platforms.find(p=>p.id===id).login_open=false;return {cancelled:true}},
        fetch_notes:({platform})=>{const p=state.platforms.find(p=>p.id===platform);p.count=3;p.complete=true;state.current_task={...task('fetch'),status:'succeeded',stage:'任务已完成。',completed:3,succeeded:3};return state.current_task},
        export_notes:()=>state.current_task=task('export'),
        migrate_notes:()=>state.current_task=task('migrate'),
        cancel_task:()=>{state.current_task={...state.current_task,status:'cancelled',stage:'操作已取消。'};window.onNoteBridgeTask?.(state.current_task);return {requested:true}},
        window_action:()=>({done:true}),open_directory:()=>({opened:true}),open_task_output:()=>({opened:true})
      };
      window.pywebview={api:Object.fromEntries(Object.entries(handlers).map(([name,fn])=>[name,async(...args)=>{calls.push({name,args});return {ok:true,data:fn(...args)}}]))};
    });
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(origin);await page.getByText('正式版 · 本地处理，数据由你掌握').waitFor();
    async function capture(name){
      await page.evaluate(()=>Promise.all(document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{}))));
      evidence.states[name]=await page.locator('dialog[open],dialog[open] h2,dialog[open] button,.login-platform-mark,.format-card,.mode-switch,progress').evaluateAll(nodes=>nodes.map(node=>{
        const r=node.getBoundingClientRect(),s=getComputedStyle(node);
        return {tag:node.tagName,label:node.getAttribute('aria-label')||node.textContent.trim(),x:r.x,y:r.y,width:r.width,height:r.height,fontSize:s.fontSize,borderRadius:s.borderRadius};
      }));
      await page.screenshot({path:path.join(out,name+'.png')});evidence.screenshots.push(name+'.png');
    }
    assert.equal(await page.locator('.platform-card').count(),7);await capture('01-export-initial');
    evidence.geometry=await page.evaluate(()=>Object.fromEntries(['.titlebar','.tabs','.export-panel','.platform-card','.platform-name','.platform-logo','.login','.panel-action .primary'].map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return[selector,{x:r.x,y:r.y,width:r.width,height:r.height}]})));
    async function allCards(target){return target.locator('.platform-card').evaluateAll(cards=>cards.map(card=>Object.fromEntries(
      Object.entries({card,name:card.querySelector('.platform-name'),logo:card.querySelector('.platform-logo'),login:card.querySelector('.login')})
        .map(([key,node])=>{const r=node.getBoundingClientRect();return[key,{x:r.x,y:r.y,width:r.width,height:r.height}]}))))}
    evidence.exportCards=await allCards(page);
    await page.getByRole('button',{name:'选择小米笔记',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'选择小米笔记',exact:true}).getAttribute('aria-pressed'),'true');await capture('02-platform-selected');
    const loginLayouts=[];
    for(const name of ['小米笔记','OPPO笔记','vivo笔记','华为备忘录','荣耀笔记','魅族笔记','WPS便签']){
      await page.getByRole('button',{name:name+'登录',exact:true}).click();
      await capture('login-'+name);
      const panel=await page.locator('.login-modal').boundingBox(),mark=await page.locator('.login-platform-mark').boundingBox();
      assert.equal(panel.width,768);assert.equal(mark.width,64);assert.equal(mark.height,64);
      assert(Math.abs(mark.x+mark.width/2-600)<1,'平台图标应居中');
      loginLayouts.push({platform:name,panel,mark});
      await page.getByRole('button',{name:'取消登录',exact:true}).click();
    }
    assert.equal(await page.evaluate(()=>window.__test.calls.filter(c=>c.name==='login_platform').length),0,'取消提示不能启动官方会话');
    evidence.loginLayouts=loginLayouts;
    await page.getByRole('button',{name:'小米笔记登录',exact:true}).click();await capture('03-login-prompt');
    await page.getByRole('button',{name:'确定并前往登录'}).click();
    await page.getByRole('button',{name:'已完成登录，检查账号'}).click();
    await page.getByRole('button',{name:'获取笔记',exact:true}).click();
    await page.getByRole('button',{name:'完成',exact:true}).click();
    await page.locator('.format-grid').waitFor();await capture('04-format-options');
    await page.getByRole('button',{name:'多文件',exact:true}).click();
    await page.locator('.format-card').filter({hasText:'HTML'}).click();
    await page.locator('.export-bottom').getByRole('button',{name:'导出笔记',exact:true}).click();
    await page.getByRole('heading',{name:'正在处理',exact:true}).waitFor();await capture('05-progress');
    const exportCall=await page.evaluate(()=>window.__test.calls.find(c=>c.name==='export_notes'));
    assert.deepEqual(exportCall.args[0],{platform:'xiaomi',format:'html',multi_file:true});
    await page.evaluate(()=>window.__test.setTask({...window.__test.state.current_task,status:'succeeded',stage:'任务已完成。',completed:3,succeeded:3,output_path:'测试导出目录'}));
    await page.getByRole('heading',{name:'已完成',exact:true}).waitFor();await capture('06-success');
    await page.evaluate(()=>window.__test.setTask({...window.__test.state.current_task,status:'failed',stage:'测试网络不可用',completed:0,succeeded:0,output_path:null,issues:[{note_id:'synthetic',code:'network_error',message:'网络请求失败，请检查网络连接后重试。'}]}));
    await page.getByRole('heading',{name:'操作失败',exact:true}).waitFor();await capture('07-failure');
    await page.getByRole('button',{name:'完成',exact:true}).click();
    await page.getByRole('button',{name:'迁移笔记',exact:true}).click();
    assert.equal(await page.locator('.platform-card').count(),14);
    assert.equal(await page.getByRole('button',{name:'小米笔记已登录',exact:true}).count(),2,'两页共享同一登录状态');
    await capture('08-migrate');
    evidence.migrationGeometry=await page.locator('.platform-card').first().boundingBox();
    const baseline=await context.newPage();
    await baseline.goto(origin);await baseline.getByRole('button',{name:'迁移笔记',exact:true}).click();
    await baseline.locator('.migration-panel').waitFor();
    evidence.migrationCards=await allCards(baseline);
    await baseline.screenshot({path:path.join(out,'09-migration-initial.png')});
    await baseline.close();
    for(const viewport of [{width:960,height:640},{width:800,height:600}]){
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'缩小视口不能横向溢出');
    }
    assert.deepEqual(errors,[]);
    evidence.checks=['7 export cards','selection state','official login flow contract','shared sessions','format and multi-file payload','progress/success/failure states','14 migration cards','no horizontal overflow','no uncaught JS exceptions'];
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));
    console.log(JSON.stringify({kind:evidence.kind,checks:evidence.checks,screenshots:evidence.screenshots,
      export_cards:evidence.exportCards.length,migration_cards:evidence.migrationCards.length}));
  }finally{await browser.close();server.close()}
})().catch(error=>{console.error(error);server.close();process.exitCode=1});
