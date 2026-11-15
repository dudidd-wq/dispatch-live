// Starts the real server and checks the deck in a headless browser:
// both maps exist with history, without history, and when Oref is down.
const { spawn } = require('child_process');
const path = require('path');
const { chromium } = require('playwright');
const ROOT = path.join(__dirname, '..');
const PORT = process.env.PORT || '3187';

(async () => {
  const srv = spawn(process.env.PYTHON || 'python3', ['server.py'], {
    cwd: ROOT, env: { ...process.env, PORT, NOOPEN: '1', NOAUTOCLOSE: '1' }, stdio: 'ignore' });
  let failed = false;
  const fail = m => { console.error('FAIL ' + m); failed = true; };
  let browser;
  try {
    await new Promise(r => setTimeout(r, 2500));
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
    const tag = await page.evaluate(() => [DL_BUILD, document.querySelector('.build-tag').textContent]);
    if ('v' + tag[0] !== tag[1]) fail('build tag mismatch ' + tag.join(' / '));
    if (errors.length) fail('page errors: ' + errors.slice(0, 3).join(' | '));
  } catch (e) { fail(e.message); }
  finally { if (browser) await browser.close(); srv.kill(); }
  console.log(failed ? 'RENDER TEST FAILED' : 'render test OK');
  process.exit(failed ? 1 : 0);
})();
