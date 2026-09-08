/* Local source-mode interaction checks; synthetic bridge responses never ship. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const out = path.join(root, '.private/evidence/migration-selection-ui');
fs.mkdirSync(out, {recursive: true});
const evidence = {kind: 'migration-selection-synthetic-ui', formal_acceptance: false,
  cloud_requests: 0, full_build: false, checks: [], screenshots: [], errors: []};
(async () => {
  const {createServer} = await import(pathToFileURL(path.join(root, 'ui/node_modules/vite/dist/node/index.js')).href);
  const server = await createServer({root: path.join(root, 'ui'), configFile: false,
    cacheDir: path.join(out, 'vite-cache'), logLevel: 'error', server: {host: '127.0.0.1', port: 0, hmr: false}});
  let browser;
  try {
    await server.listen();
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
    const context = await browser.newContext({viewport: {width: 1200, height: 800}, deviceScaleFactor: 1, locale: 'zh-CN', reducedMotion: 'reduce'});
    await context.route('**/*', route => new URL(route.request().url()).origin === origin && route.request().method() === 'GET' ? route.continue() : route.abort());
    await context.addInitScript(() => {
      const rows = [
        {id: 'one', title: '同名', summary: '第一条正文', compatible: true, reason: '', code: '', already_migrated: false, warnings: []},
        {id: 'two', title: '同名', summary: '第二条正文', compatible: true, reason: '', code: '', already_migrated: false, warnings: []},
        {id: 'audio', title: '音频笔记', summary: '保留录音原件', compatible: false, reason: '含音频附件，当前迁入只支持图片，可先本地导出。', code: 'unsupported_attachment', already_migrated: false, warnings: []},
      ];
      const ids = ['xiaomi', 'oppo', 'vivo', 'huawei', 'honor', 'meizu', 'wps'];
      const state = {name: '笔记互迁', version: 'synthetic', platforms: ids.map(id => ({id, logged_in: true, logging_in: false, count: 0, complete: false, capability: 'experimental'})), current_task: null, has_export: false, release_ready: false};
      const task = (operation, status) => ({id: 'synthetic-' + operation, operation, status, stage: status === 'running' ? '正在读取测试笔记' : '任务已完成。', completed: status === 'running' ? 0 : 3, total: 3, succeeded: status === 'running' ? 0 : 3, skipped: 0, issues: [], started_at: '2026-09-07T00:00:00Z'});
      const test = window.__migrationSelectionTest = {calls: [], deferred: false, writeAvailable: true, previewFailure: false,
        completeRead() {state.current_task = task('fetch', 'succeeded'); const source = state.platforms.find(p => p.id === 'vivo'); source.count = 3; source.complete = true; window.onNoteBridgeTask?.(state.current_task)}};
      const handlers = {
        get_app_state: () => state,
        fetch_notes: () => state.current_task = task('fetch', test.deferred ? 'running' : 'succeeded'),
        preview_migration: () => {
          if (test.previewFailure) return {ok: false, error: {code: 'source_incomplete', message: '来源未完整获取，请重新读取。'}};
          return {token: 'synthetic-preview-token', source: 'vivo', target: 'meizu', total: 3, items: rows,
            write_available: test.writeAvailable, blocked_reason: test.writeAvailable ? '' : '该平台的迁入功能尚未开放；预检和选择不会执行写入。'};
        },
        migrate_notes: payload => state.current_task = {...task('migrate', 'partial'), total: payload.note_ids.length,
          completed: payload.note_ids.length, succeeded: payload.note_ids.length,
          issues: rows.filter(row => !payload.note_ids.includes(row.id)).map(row => ({note_id: row.id, code: 'not_selected', message: `${row.title}未迁移：${row.reason || '未选择'}。`}))},
      };
      window.pywebview = {api: Object.fromEntries(Object.entries(handlers).map(([name, fn]) => [name, async (...args) => {
        test.calls.push({name, args}); const data = fn(...args); return data?.ok === false ? data : {ok: true, data};
      }]))};
    });
    async function pageFor(config = {}) {
      const page = await context.newPage(); page.on('pageerror', error => evidence.errors.push(error.message));
      await page.goto(origin);
      await page.evaluate(config => Object.assign(window.__migrationSelectionTest, config), config);
      await page.getByRole('button', {name: '迁移笔记', exact: true}).click();
      await page.locator('.migration-side').nth(0).getByRole('button', {name: '选择vivo笔记', exact: true}).click();
      await page.locator('.migration-side').nth(1).getByRole('button', {name: '选择魅族笔记', exact: true}).click();
      await page.getByRole('button', {name: '开始迁移', exact: true}).click();
      return page;
    }
    async function capture(page, name) {
      await page.screenshot({path: path.join(out, name + '.png')}); evidence.screenshots.push(name + '.png');
    }
    const page = await pageFor({deferred: true});
    await page.getByRole('heading', {name: '正在处理', exact: true}).waitFor();
    assert.equal(await page.evaluate(() => window.__migrationSelectionTest.calls.filter(call => call.name === 'preview_migration').length), 0);
    await page.evaluate(() => window.__migrationSelectionTest.completeRead());
    await page.getByRole('heading', {name: '选择要迁移的笔记', exact: true}).waitFor();
    assert.equal(await page.locator('.selection-row input:checked').count(), 3, 'Default must retain the all-notes intent, including visible incompatible rows');
    assert.equal(await page.getByRole('button', {name: '确认迁移 3 条', exact: true}).isDisabled(), true);
    assert.equal(await page.getByText('含音频附件，当前迁入只支持图片，可先本地导出。', {exact: true}).isVisible(), true);
    await capture(page, '01-incompatible-default-selected');
    await page.getByRole('button', {name: '仅选兼容笔记', exact: true}).click();
    assert.equal(await page.locator('.selection-row input:checked').count(), 2);
    assert.equal(await page.locator('.selection-row').filter({hasText: '音频笔记'}).locator('input').isChecked(), false);
    await page.locator('.selection-row').filter({hasText: '第一条正文'}).locator('input').uncheck();
    assert.equal(await page.getByText('共 3 条 · 已选 1 条 · 本次不迁移 2 条', {exact: true}).isVisible(), true);
    await capture(page, '02-explicit-single-selection');
    await page.getByRole('button', {name: '确认迁移 1 条', exact: true}).click();
    await page.getByRole('heading', {name: '部分完成', exact: true}).waitFor();
    assert.equal(await page.locator('.issue-list').getByText('音频笔记未迁移：含音频附件，当前迁入只支持图片，可先本地导出。。', {exact: true}).isVisible(), true);
    const calls = await page.evaluate(() => window.__migrationSelectionTest.calls);
    assert.equal(calls.filter(call => call.name === 'fetch_notes').length, 1, 'Confirming the selection must not fetch all source notes again');
    assert.equal(calls.filter(call => call.name === 'preview_migration').length, 1);
    assert.deepEqual(calls.find(call => call.name === 'migrate_notes').args[0], {source: 'vivo', target: 'meizu', note_ids: ['two'], preview_token: 'synthetic-preview-token'});
    await capture(page, '03-exclusions-remain-partial');
    await page.close();

    const blocked = await pageFor({writeAvailable: false});
    await blocked.getByRole('heading', {name: '选择要迁移的笔记', exact: true}).waitFor();
    await blocked.getByRole('button', {name: '仅选兼容笔记', exact: true}).click();
    assert.equal(await blocked.getByRole('button', {name: '确认迁移 2 条', exact: true}).isDisabled(), true);
    assert.equal(await blocked.getByText('该平台的迁入功能尚未开放；预检和选择不会执行写入。', {exact: true}).isVisible(), true);
    await capture(blocked, '04-production-gate-retained');
    await blocked.getByRole('button', {name: '取消', exact: true}).click();
    assert.equal(await blocked.locator('dialog[open]').count(), 0);
    assert.equal(await blocked.evaluate(() => window.__migrationSelectionTest.calls.filter(call => call.name === 'migrate_notes').length), 0);
    await blocked.close();

    const incomplete = await pageFor({previewFailure: true});
    await incomplete.getByRole('heading', {name: '预检未完成', exact: true}).waitFor();
    assert.equal(await incomplete.getByText('来源未完整获取，请重新读取。', {exact: true}).isVisible(), true);
    assert.equal(await incomplete.evaluate(() => window.__migrationSelectionTest.calls.filter(call => call.name === 'migrate_notes').length), 0);
    await incomplete.close();
    assert.deepEqual(evidence.errors, []);
    evidence.checks = ['Preview waits for the source read', 'Default all-selection retains visible incompatible notes',
      'Explicit compatible-only selection and duplicate-title differentiation', 'Only selected IDs and matching token are submitted',
      'Exactly one full source fetch', 'Excluded notes remain visible in partial result', 'Production gate remains closed',
      'Cancel and incomplete-preview paths never migrate', 'No uncaught JavaScript errors'];
    fs.writeFileSync(path.join(out, 'evidence.json'), JSON.stringify(evidence, null, 2));
    console.log(JSON.stringify({kind: evidence.kind, checks: evidence.checks, formal_acceptance: false}));
  } finally { if (browser) await browser.close(); await server.close(); }
})().catch(error => {console.error(error); process.exitCode = 1});
