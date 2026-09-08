/* Capture the built UI with synthetic data only; never connects to a vendor account.
   Example: npm run build --prefix ui; node scripts/capture-portfolio.cjs
   Requires Playwright/Chromium; PLAYWRIGHT_MODULE_PATH and CHROME_PATH may override locations.
   Output: .private/portfolio-evidence (review before copying any media into assets). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'ui', 'dist');
const out = path.join(root, '.private', 'portfolio-evidence');
assert(fs.existsSync(path.join(dist, 'index.html')), 'Build the UI first.');
fs.mkdirSync(out, {recursive: true});
const mime = {'.js':'text/javascript', '.css':'text/css', '.html':'text/html', '.svg':'image/svg+xml'};
const server = http.createServer((req, res) => {
  if (req.url === '/favicon.ico') { res.writeHead(204); res.end(); return; }
  const file = path.resolve(dist, '.' + (req.url === '/' ? '/index.html' : req.url.split('?')[0]));
  if (!file.startsWith(dist + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    res.writeHead(404); res.end(); return;
  }
  res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const context = await browser.newContext({viewport:{width:1200,height:960},
      deviceScaleFactor:1, locale:'zh-CN', reducedMotion:'reduce',
      recordVideo:{dir:out,size:{width:1200,height:960}}});
    const externalRequests = [];
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      externalRequests.push(new URL(route.request().url()).origin);
      return route.abort();
    });
    await context.addInitScript(() => {
      const ids = ['xiaomi','oppo','vivo','huawei','honor','meizu','wps'];
      const state = {name:'笔记互迁',version:'1.0.0',release_channel:'stable',release_ready:false,
        platforms:ids.map(id => ({id,logged_in:['xiaomi','wps'].includes(id),logging_in:false,
          login_open:false,count:0,complete:false,capability:'synthetic'})),current_task:null,has_export:false};
      const calls = [];
      const task = (operation, status, stage, total) => ({id:'synthetic-'+operation,operation,status,stage,
        completed:status==='succeeded'?total:0,total,succeeded:status==='succeeded'?total:0,skipped:0,
        issues:[],started_at:'2026-09-08T00:00:00Z',output_path:null});
      const handlers = {
        get_app_state:() => state,
        fetch_notes:({platform}) => {
          Object.assign(state.platforms.find(p => p.id===platform),{count:3,complete:true});
          return state.current_task = task('fetch','succeeded','演示：已载入 3 条合成样例，未读取云端。',3);
        },
        preview_migration:({source,target}) => ({token:'synthetic-preview',source,target,total:3,
          write_available:true,blocked_reason:'',items:[
            {id:'demo-study',title:'合成样例 · 开学计划',summary:'课程安排、书单与学习目标。',compatible:true,reason:'',code:'',already_migrated:false,warnings:[]},
            {id:'demo-project',title:'合成样例 · 项目复盘',summary:'记录需求、取舍与验证结果。',compatible:true,reason:'',code:'',already_migrated:false,warnings:['演示格式差异：代码样式可能转为普通文字。']},
            {id:'demo-handwriting',title:'合成样例 · 手写草稿',summary:'用于展示不兼容内容的处理。',compatible:false,reason:'演示：专有手写内容不支持迁入，请保留来源。',code:'unsupported',already_migrated:false,warnings:[]}
          ]}),
        migrate_notes:({note_ids}) => {
          state.current_task = task('migrate','needs_review','演示：写入结果不明，停止自动重试。',note_ids.length);
          state.current_task.issues = [{note_id:'demo-study',code:'write_uncertain',message:'合成场景：目标可能已收到请求，需要人工核对。本演示没有发送云端请求。'}];
          return state.current_task;
        },
        window_action:() => ({done:false})
      };
      window.__portfolio = {calls};
      window.pywebview = {api:Object.fromEntries(Object.entries(handlers).map(([name,fn]) =>
        [name,async(...args) => {calls.push({name,args});return {ok:true,data:fn(...args)};}]))};
      window.addEventListener('DOMContentLoaded', () => {
        const notice = document.createElement('aside');
        notice.textContent = '界面演示 / UI DEMO  ·  合成数据与模拟桥接  ·  未连接真实账号，未发送云端请求';
        notice.style.cssText = 'height:36px;display:flex;align-items:center;justify-content:center;background:#283346;color:#fff;font:12px Microsoft YaHei,sans-serif;letter-spacing:.5px;';
        document.body.prepend(notice);
        const style = document.createElement('style');
        style.textContent = '.app-shell{min-height:calc(100vh - 36px)}';
        document.head.append(style);
      });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin);
    await page.getByText('正式版 · 本地处理，数据由你掌握').waitFor();
    const screenshots = [];
    async function capture(name) {
      await page.screenshot({path:path.join(out,name+'.png')});
      screenshots.push(name+'.png');
      // Intentional hold for a readable demonstration video, not a readiness wait.
      await page.waitForTimeout(2500);
    }
    await page.getByRole('button',{name:'选择小米笔记',exact:true}).click();
    await page.getByRole('button',{name:'获取笔记',exact:true}).click();
    await page.getByRole('button',{name:'完成',exact:true}).click();
    await page.locator('.format-grid').waitFor();
    await page.locator('.format-card').filter({hasText:'HTML'}).click();
    await capture('01-export-formats');
    await page.getByRole('button',{name:'迁移笔记',exact:true}).click();
    await page.locator('.migration-side').nth(1).getByRole('button',{name:'选择WPS便签',exact:true}).click();
    await capture('02-migration-direction');
    await page.getByRole('button',{name:'开始迁移',exact:true}).click();
    await page.getByRole('heading',{name:'选择要迁移的笔记',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'确认迁移 3 条',exact:true}).isEnabled(),false);
    await capture('03-compatibility');
    await page.getByRole('button',{name:'仅选兼容笔记',exact:true}).click();
    await capture('04-selection');
    await page.getByRole('button',{name:'确认迁移 2 条',exact:true}).click();
    await page.getByRole('heading',{name:'需要核对',exact:true}).waitFor();
    await capture('05-needs-review');
    const calls = await page.evaluate(() => window.__portfolio.calls);
    assert.deepEqual(calls.find(call => call.name==='migrate_notes').args[0].note_ids,
      ['demo-study','demo-project']);
    assert.deepEqual(errors,[]);
    assert.deepEqual(externalRequests,[]);
    const evidence = {kind:'real-built-ui-with-synthetic-bridge',formal_acceptance:false,
      captured_at:new Date().toISOString(),viewport:{width:1200,height:960},screenshots,
      checks:['format choices displayed','direction selection','incompatible selection blocks confirmation',
        'compatible selection excludes unsupported item','uncertain outcome shows review guidance'],
      page_errors:errors,external_requests:externalRequests,cloud_requests:0};
    const video = page.video();
    await context.close();
    await video.saveAs(path.join(out,'demo.webm'));
    fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));
    console.log(JSON.stringify(evidence));
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => {console.error(error);server.close();process.exitCode=1;});
