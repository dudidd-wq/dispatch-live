// Starts the real server and checks the deck in a headless browser:
// both maps exist with history, without history, and when Oref is down.
const { spawn } = require('child_process');
const path = require('path');
const { chromium } = require('playwright');
const fs = require('fs'), os = require('os');
const SRC = path.join(__dirname, '..');
// a scratch copy: the settings test writes config.local.json and must not touch the checkout
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-render-'));
for (const f of ['index.html', 'server.py', 'brain.py', 'dlconfig.py', 'dlarchive.py']) fs.copyFileSync(path.join(SRC, f), path.join(ROOT, f));
const PORT = process.env.PORT || '3187';

(async () => {
  const srv = spawn(process.env.PYTHON || 'python3', ['server.py'], {
    cwd: ROOT, env: { ...process.env, PORT, NOOPEN: '1', NOAUTOCLOSE: '1' }, stdio: 'ignore' });
  let failed = false;
  const fail = m => { console.error('FAIL ' + m); failed = true; };
  let browser;
  try {
    await new Promise(r => setTimeout(r, 2500));
    // secrets and runtime state must never be reachable as static files
    fs.writeFileSync(path.join(ROOT, 'google.key'), 'not-a-real-key');
    fs.writeFileSync(path.join(ROOT, 'config.local.json'), '{}');
    for (const f of ['google.key', 'config.local.json', '.gitignore', 'data/state.json', 'data/archive.db', 'logs/server.log']) {
      const r = await fetch('http://127.0.0.1:' + PORT + '/' + f);
      if (r.status !== 404) fail('/' + f + ' is served as a static file (HTTP ' + r.status + ')');
    }
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4000);
    const rows = [
      { alertDate: '2026-11-13 03:00:07', title: 'האירוע הסתיים', data: 'נחל עוז', category: 13 },
      { alertDate: '2026-11-13 02:56:06', title: 'ירי רקטות וטילים', data: 'נחל עוז', category: 1 }];
    for (const [name, arg, wantRows] of [['oref-down', null, 0], ['quiet', [], 0], ['history', rows, 2]]) {
      const r = await page.evaluate(a => { renderAlertsBody(a); return new Promise(res => setTimeout(() => res({
        cells: document.querySelectorAll('.map-stack .map-cell').length,
        alertMap: document.querySelectorAll('.alert-map').length,
        traffic: !!document.getElementById('liveTrafficCell'),
        rows: document.querySelectorAll('.hist-row').length }), 300)); }, arg);
      console.log(name, JSON.stringify(r));
      if (r.cells !== 2 || r.alertMap !== 1 || !r.traffic) fail(name + ': maps missing');
      if (r.rows !== wantRows) fail(name + ': expected ' + wantRows + ' history rows, got ' + r.rows);
    }
    // health panel
    await page.waitForTimeout(3500);
    await page.click('#healthPill');
    await page.waitForTimeout(1500);
    const hp = await page.evaluate(() => ({ rows: document.querySelectorAll('#healthBody .hl-table tr').length,
      sub: document.getElementById('healthSub').textContent }));
    console.log('health', JSON.stringify(hp));
    if (hp.rows < 3) fail('health panel is empty');
    await page.click('#healthClose');

    // archive: the deck's own tension readings must come back as a summary and a chart
    const posted = await page.evaluate(async () => {
      const r = await fetch('/archive/tension', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ score: 37, alerts: 1, idf: 2, news: 3 }) });
      return r.status;
    });
    if (posted !== 200) fail('POST /archive/tension answered ' + posted);
    await page.click('#archiveBtn');
    await page.waitForTimeout(1500);
    const ar = await page.evaluate(() => ({ cards: document.querySelectorAll('#archiveBody .arc-card').length,
      charts: document.querySelectorAll('#archiveBody svg.arc-chart').length,
      text: document.getElementById('archiveBody').textContent.includes('37') }));
    console.log('archive', JSON.stringify(ar));
    if (ar.cards < 5 || ar.charts < 2 || !ar.text) fail('archive panel incomplete');
    await page.click('#archiveClose');

    // settings: change a threshold, save, and find it again after the reload
    await page.click('#settingsBtn');
    await page.waitForSelector('#setMinDelay');
    await page.fill('#setMinDelay', '13');
    await page.fill('#setCustomCh', 'Test | https://example.com/live/t.m3u8');
    await page.click('#setSave');
    await page.waitForTimeout(5000);
    const st = await page.evaluate(() => ({ min: CFG.traffic.minDelay,
      custom: CHANNELS.filter(c => c.custom).map(c => c.name), on: CHANNELS.filter(c => c.custom).every(c => state.tvOn.has(c.id)) }));
    console.log('settings', JSON.stringify(st));
    if (st.min !== 13) fail('saved threshold did not survive the reload');
    if (st.custom[0] !== 'Test' || !st.on) fail('custom channel missing after save');

    // a broken alert feed must never read as "quiet"
    for (const [name, body, wantDown] of [['quiet', '[]', false], ['active', '{"id":"1","title":"t","data":["x"],"desc":"d"}', false],
                                          ['html', '<!DOCTYPE html><title>moved</title>', true], ['null', 'null', true],
                                          ['other-json', '{"message":"moved"}', true]]) {
      await page.route('**/oref/alerts', r => r.fulfill({ status: 200, contentType: 'application/json', body }));
      await page.evaluate(() => orefConn(true));
      for (let i = 0; i < 7; i++) await page.evaluate(() => pollAlerts());      // the deck declares an outage after 6 misses
      const down = await page.evaluate(() => !!state.orefDown);
      console.log('alert-shape', name, 'down=' + down);
      if (down !== wantDown) fail('alert feed shape "' + name + '": orefDown=' + down + ', expected ' + wantDown);
      await page.unroute('**/oref/alerts');
    }
    await page.evaluate(() => { hideTakeover && hideTakeover(); });

    // an old answer must be labelled as old
    await page.evaluate(async () => { await tfetch('/quakes'); });
    await page.route('**/quakes', r => r.fulfill({ status: 200, contentType: 'application/json', body: '[]',
      headers: { 'X-DL-Stale': '1800' } }));
    await page.evaluate(async () => { await tfetch('/quakes'); });
    const bar = await page.evaluate(() => { const b = document.getElementById('staleBar'); return b && !b.hidden ? b.textContent : ''; });
    console.log('stale bar:', bar);
    if (!bar.includes('רעידות אדמה')) fail('stale data is not labelled');
    await page.unroute('**/quakes');

    // hostile stored preferences must not take the page down
    const ctx2 = await browser.newContext();
    const p2 = await ctx2.newPage();
    const errors2 = [];
    p2.on('pageerror', e => errors2.push(e.message));
    await p2.addInitScript(() => { try { localStorage.setItem('dl-tv', '"ch11"'); localStorage.setItem('dl-news-off', '{}'); } catch {} });
    await p2.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'domcontentloaded' });
    await p2.waitForTimeout(2500);
    const alive = await p2.evaluate(() => typeof state === 'object' && state.tvOn.size > 0 && typeof pollAlerts === 'function');
    console.log('hostile prefs: alive=' + alive, errors2.slice(0, 2));
    if (!alive || errors2.length) fail('bad stored preferences broke the page: ' + errors2.join(' | '));
    await ctx2.close();

    const tag = await page.evaluate(() => [DL_BUILD, document.querySelector('.build-tag').textContent]);
    if ('v' + tag[0] !== tag[1]) fail('build tag mismatch ' + tag.join(' / '));
    if (errors.length) fail('page errors: ' + errors.slice(0, 3).join(' | '));
  } catch (e) { fail(e.message); }
  finally { if (browser) await browser.close(); srv.kill(); }
  console.log(failed ? 'RENDER TEST FAILED' : 'render test OK');
  process.exit(failed ? 1 : 0);
})();
