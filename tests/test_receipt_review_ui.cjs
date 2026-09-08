/* A local-only UI check; no real session or cloud transport is available here. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const out = path.join(root, '.private/evidence/receipt-review-ui-' + randomUUID());
fs.mkdirSync(out, {recursive: true});
const evidence = {kind: 'receipt-review-synthetic-ui', formal_acceptance: false, cloud_requests: 0, checks: []};
(async () => {
  const {createServer} = await import(pathToFileURL(path.join(root, 'ui/node_modules/vite/dist/node/index.js')).href);
  const server = await createServer({root: path.join(root, 'ui'), configFile: false,
    cacheDir: path.join(out, 'vite-cache'), logLevel: 'error', server: {host: '127.0.0.1', port: 0, hmr: false}});
  let browser;
  try {
    await server.listen();
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
    const context = await browser.newContext({viewport: {width: 1200, height: 800}, locale: 'zh-CN', reducedMotion: 'reduce'});
    await context.route('**/*', route => new URL(route.request().url()).origin === origin && route.request().method() === 'GET' ? route.continue() : route.abort());
    await context.addInitScript(() => {
      const task = {id: 'synthetic-task', operation: 'migrate', status: 'needs_review', stage: '写入结果不明，请先核对。',
        completed: 0, total: 1, succeeded: 0, skipped: 0, issues: [], started_at: '2026-09-07T00:00:00Z'};
      const state = {name: '笔记互迁', version: 'synthetic', platforms: ['xiaomi', 'oppo', 'vivo', 'huawei', 'honor', 'meizu', 'wps'].map(id =>
        ({id, logged_in: true, logging_in: false, count: 2, complete: true, capability: 'experimental'})),
        current_task: task, has_export: false, release_channel: 'stable', release_ready: false};
      const test = window.__receiptReviewTest = {calls: [], empty: false};
      window.pywebview = {api: {
        get_app_state: async () => ({ok: true, data: state}),
        review_migration: async payload => {
          test.calls.push(payload);
          return {ok: true, data: {...payload, cloud_checked: false, source_cache_complete: true,
            target_cache_complete: false, cached_notes: 2, unmatched_cached_notes: test.empty ? 2 : 1,
            items: test.empty ? [] : [
              {id: 'source-1', title: '合成笔记 <img src=x onerror=alert(1)>', version: 'current', status: 'uncertain',
                remote_ids: ['target-1'], cached_remote_ids: ['target-1'], resource_states: ['allocated', 'uploaded']},
              {id: 'source-1', title: '合成笔记 <img src=x onerror=alert(1)>', version: 'previous', status: 'sending',
                remote_ids: ['old-target-1'], cached_remote_ids: [], resource_states: ['allocated']},
              {id: 'missing-source', title: '', version: 'source_missing', status: 'sending', remote_ids: [], cached_remote_ids: [], resource_states: []}
            ]}};
        }
      }};
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin);
    await page.getByRole('button', {name: '迁移记录', exact: true}).click();
    await page.getByRole('heading', {name: '选择要核对的迁移方向', exact: true}).waitFor();
    assert.equal(await page.evaluate(() => window.__receiptReviewTest.calls.length), 0);
    await page.getByRole('button', {name: '知道了', exact: true}).click();
    evidence.checks.push('direction-required-before-local-review');
    await page.locator('.migration-side').nth(0).getByRole('button', {name: '选择vivo笔记', exact: true}).click();
    await page.locator('.migration-side').nth(1).getByRole('button', {name: '选择华为备忘录', exact: true}).click();
    await page.getByRole('button', {name: '迁移记录', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: '迁移记录 · 只读查看', exact: true});
    await dialog.waitFor();
    assert.equal(await dialog.locator('.receipt-row').count(), 3);
    assert.equal(await dialog.locator('.receipt-row img').count(), 0, 'Titles must stay escaped text');
    assert.equal(await dialog.getByText('写入结果不明，待核对', {exact: true}).isVisible(), true);
    assert.equal(await dialog.getByText(/目标本地缓存命中 1 \/ 1/).isVisible(), true);
    assert.equal(await dialog.getByText(/未取得目标标识/).isVisible(), true);
    assert.equal(await dialog.getByText(/旧版本回执：与当前缓存正文或元数据不同/).isVisible(), true);
    assert.equal(await dialog.getByRole('heading', {name: '来源未缓存，旧标题未保存', exact: true}).isVisible(), true);
    assert.equal(await dialog.getByText(/当前来源缓存 2 条，其中 1 条有关联回执，另有 1 条没有匹配回执；共显示 3 条版本记录/).isVisible(), true);
    assert.equal(await dialog.getByText(/无法判断是否已删除/).isVisible(), true);
    assert.equal(await dialog.getByRole('button').count(), 2, 'Only modal close controls are offered');
    assert.equal(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth), true);
    await page.screenshot({path: path.join(out, 'records.png')});
    assert.deepEqual(await page.evaluate(() => window.__receiptReviewTest.calls), [{source: 'vivo', target: 'huawei'}]);
    evidence.checks.push('unknown-status-resource-states-and-cache-limit-displayed', 'no-mutation-controls-or-title-html',
      'old-version-and-missing-source-remain-explicit', 'multiple-versions-count-distinct-cached-sources');
    await dialog.getByRole('button', {name: '关闭', exact: true}).last().click();
    await page.evaluate(() => {window.__receiptReviewTest.empty = true});
    await page.getByRole('button', {name: '迁移记录', exact: true}).click();
    await dialog.getByText('当前范围没有可关联的迁移回执。', {exact: true}).waitFor();
    assert.equal(await dialog.getByText(/列表为空也不代表从未迁移/).isVisible(), true);
    await page.screenshot({path: path.join(out, 'empty-scope.png')});
    evidence.checks.push('empty-scope-is-not-proof-of-no-history');
    await dialog.getByRole('button', {name: '关闭', exact: true}).last().click();
    await page.locator('.task-strip').click();
    await page.getByRole('dialog', {name: '需要核对', exact: true}).getByText(/点击底部“迁移记录”查看本地回执/).waitFor();
    evidence.checks.push('interrupted-task-explains-read-only-review');
    assert.deepEqual(errors, []);
  } catch (error) {
    evidence.error = {name: error.name, message: error.message};
    throw error;
  } finally {
    if (browser) await browser.close();
    await server.close();
    fs.writeFileSync(path.join(out, 'evidence.json'), JSON.stringify(evidence, null, 2));
    process.stdout.write(JSON.stringify({evidence: path.relative(root, path.join(out, 'evidence.json')), checks: evidence.checks.length}) + '\n');
  }
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1});
