/* Synthetic source-mode progress and geometry checks; never run the reference program. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const out = path.join(root, '.private/evidence/migration-progress-ui-' + randomUUID());
fs.mkdirSync(out, {recursive: true});
const evidence = {kind: 'migration-progress-synthetic-ui', formal_acceptance: false,
  original_executable_run: false, original_frontend_run: false, cloud_requests: 0, checks: [],
  limitation: 'No runnable original migration-progress screenshot baseline; progress geometry checks use static source specifications only.'};
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
      const state = {name: '笔记互迁', version: 'synthetic', platforms: ['xiaomi', 'oppo', 'vivo', 'huawei', 'honor', 'meizu', 'wps'].map(id =>
        ({id, logged_in: false, logging_in: false, count: 0, complete: false, capability: 'pending'})),
        current_task: null, has_export: false, release_channel: 'stable', release_ready: false};
      const test = window.__migrationProgressTest = {calls: [], setTask(changes) {
        state.current_task = {id: 'synthetic-migrate', operation: 'migrate', status: 'running', stage: '正在迁移合成样例。',
          completed: 1, total: 4, succeeded: 1, skipped: 0, issues: [], started_at: '2026-09-07T00:00:00Z', ...changes};
        window.onNoteBridgeTask?.(state.current_task);
      }};
      window.pywebview = {api: {
        get_app_state: async () => ({ok: true, data: state}),
        cancel_task: async id => {test.calls.push({method: 'cancel_task', id}); test.setTask({status: 'cancelled', stage: '操作已取消。'}); return {ok: true, data: {requested: true}}}
      }};
    });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin);
    await page.getByRole('button', {name: '迁移笔记', exact: true}).click();
    await page.evaluate(() => document.fonts.ready);
    const cards = () => page.locator('.platform-card').evaluateAll(nodes => nodes.map(card => Object.fromEntries(
      Object.entries({card, name: card.querySelector('.platform-name'), logo: card.querySelector('.platform-logo'), login: card.querySelector('.login')})
        .map(([key, node]) => {const r = node.getBoundingClientRect(); return [key, {x: r.x, y: r.y, width: r.width, height: r.height}]}))));
    const initial = await cards();
    assert.equal(initial.length, 14);
    // Reference evidence is deliberately excluded from public source archives.
    // An explicit optional path enables that additional private comparison.
    if (process.env.REFERENCE_GEOMETRY_PATH) {
      const reference = JSON.parse(fs.readFileSync(process.env.REFERENCE_GEOMETRY_PATH, 'utf8')).states.migration.cards;
      const deviations = initial.flatMap((card, index) => Object.keys(card).map(control => Math.max(
        ...Object.keys(card[control]).map(axis => Math.abs(card[control][axis] - reference[index][control][axis])))));
      assert.equal(deviations.length, 56);
      assert(Math.max(...deviations) <= 2, 'Previously measured initial migration controls must remain within the existing baseline');
      evidence.initial_controls = {count: deviations.length, max_deviation_css_px: Math.max(...deviations)};
      evidence.checks.push('56-existing-initial-migration-controls-preserved');
    } else {
      evidence.reference_comparison = {status: 'not_run', reason: 'REFERENCE_GEOMETRY_PATH was not supplied; private reference evidence is optional.'};
    }
    assert.equal(await page.locator('.migration-progress').count(), 0);
    await page.evaluate(() => window.__migrationProgressTest.setTask({}));
    const progress = page.getByRole('progressbar', {name: '迁移进度', exact: true});
    await progress.waitFor();
    assert.equal(await progress.getAttribute('aria-valuenow'), '25');
    assert.equal(await page.getByRole('button', {name: '开始迁移', exact: true}).isDisabled(), true);
    assert.deepEqual(await cards(), initial, 'Adding progress below the action must not move the platform controls');
    const metrics = await page.locator('.migration-progress').evaluate(node => {
      const box = element => {const r = element.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}};
      const css = getComputedStyle(node), track = node.querySelector('.migration-progress-track');
      return {panel: box(node), action: box(document.querySelector('.migration-panel .panel-action .primary')),
        label: box(node.querySelector('.migration-progress-status')), spinner: box(node.querySelector('.spinner')),
        track: box(track), fill: box(track.firstElementChild), radius: css.borderRadius, padding: css.padding,
        background: css.backgroundColor, fill_color: getComputedStyle(track.firstElementChild).backgroundColor};
    });
    assert.equal(metrics.panel.width, 448); assert.equal(metrics.panel.height, 70);
    assert.equal(metrics.panel.y - metrics.action.y - metrics.action.height, 12);
    assert.equal(metrics.panel.x + metrics.panel.width / 2, 600);
    assert.equal(metrics.spinner.width, 14); assert.equal(metrics.spinner.height, 14);
    assert.equal(metrics.label.height, 16); assert.equal(metrics.track.height, 8);
    assert.equal(metrics.fill.width, metrics.track.width / 4);
    assert.equal(metrics.radius, '16px'); assert.equal(metrics.padding, '16px');
    assert.equal(metrics.background, 'rgb(248, 250, 252)'); assert.equal(metrics.fill_color, 'rgb(10, 132, 255)');
    evidence.progress_static_spec_metrics = metrics;
    await page.locator('.migration-progress').scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(out, 'running.png')});
    evidence.checks.push('static-progress-dimensions-color-and-action-gap', 'backend-count-drives-progress');
    await page.evaluate(() => window.__migrationProgressTest.setTask({status: 'queued', total: 0, completed: 0, stage: '正在准备迁移。'}));
    assert.equal(await progress.getAttribute('aria-valuenow'), null);
    assert.equal(await progress.getAttribute('aria-valuetext'), '正在准备，尚未确定总数');
    assert.equal(await progress.locator('div').evaluate(node => node.getBoundingClientRect().width), 0);
    evidence.checks.push('unknown-total-does-not-invent-completion');
    await page.locator('.task-strip').click();
    const taskDialog = page.getByRole('dialog', {name: '准备开始', exact: true});
    await taskDialog.getByRole('button', {name: '取消任务', exact: true}).click();
    await page.getByRole('dialog', {name: '已取消', exact: true}).waitFor();
    assert.equal(await page.locator('.migration-progress').count(), 0);
    assert.deepEqual(await page.evaluate(() => window.__migrationProgressTest.calls), [{method: 'cancel_task', id: 'synthetic-migrate'}]);
    await page.getByRole('button', {name: '完成', exact: true}).click();
    evidence.checks.push('original-task-dialog-and-cancellation-retained');
    for (const status of ['succeeded', 'partial', 'failed', 'needs_review']) {
      await page.evaluate(status => window.__migrationProgressTest.setTask({status, stage: '合成结果状态。'}), status);
      assert.equal(await page.locator('.migration-progress').count(), 0);
    }
    await page.locator('.task-strip').click();
    await page.getByRole('dialog', {name: '需要核对', exact: true}).getByText(/点击底部“迁移记录”/).waitFor();
    await page.getByRole('button', {name: '完成', exact: true}).click();
    evidence.checks.push('terminal-results-hide-inline-progress-and-preserve-report');
    await page.evaluate(() => window.__migrationProgressTest.setTask({operation: 'fetch'}));
    assert.equal(await page.locator('.migration-progress').count(), 0);
    evidence.checks.push('nonmigration-task-is-not-labeled-migration-progress');
    await page.evaluate(() => window.__migrationProgressTest.setTask({}));
    for (const viewport of [{width: 960, height: 640}, {width: 800, height: 600}]) {
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const panel = await page.locator('.migration-progress').boundingBox();
      assert(panel.width <= 448 && panel.x >= 0 && panel.x + panel.width <= viewport.width);
    }
    evidence.checks.push('smaller-viewports-do-not-overflow');
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
