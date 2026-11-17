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
    // errors thrown inside an embedded third-party player (a TV channel's own page) are not ours
    page.on('pageerror', e => { if (!/Blocked a frame with origin/.test(e.message)) errors.push(e.message); });
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

    // The siren must make sound on a page nobody has touched (the deck reloads
    // itself every few hours): count real oscillators, do not stub playSiren.
    {
      const fresh = await (await browser.newContext()).newPage();
      await fresh.addInitScript(() => {
        window.__osc = 0;
        const AC = window.AudioContext || window.webkitAudioContext;
        const orig = AC.prototype.createOscillator;
        AC.prototype.createOscillator = function () { window.__osc++; return orig.call(this); };
      });
      await fresh.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'domcontentloaded' });
      await fresh.waitForTimeout(2000);
      const osc = await fresh.evaluate(() => { window.__osc = 0;
        handleAlertData({ id: 's1', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: 'היכנסו למרחב המוגן' });
        return window.__osc; });
      console.log('siren oscillators on an untouched page:', osc);
      if (!(osc > 0)) fail('the siren makes no sound on a freshly loaded page');
      await fresh.context().close();
    }

    // The classifier fails safe: body text can never turn an attack into an all-clear.
    const kinds = await page.evaluate(() => [
      { cat: '1', title: 'ירי רקטות וטילים', desc: 'היכנסו למרחב המוגן ושהו בו 10 דקות, לאחר מכן ניתן לצאת' },
      { cat: '3', title: 'רעידת אדמה', desc: 'אם לא ניתן לצאת מהמבנה היכנסו לממ"ד' },
      { cat: '7', title: 'אירוע חומרים מסוכנים', desc: 'הישארו בבתים עד להודעה כי ניתן לצאת' },
      { cat: '13', title: 'חדירת מחבלים', desc: 'אינכם יכולים לצאת' },
      { cat: '6', title: 'חדירת כלי טיס עוין', desc: 'בדקות הקרובות ייתכנו התרעות נוספות' },
      { cat: '10', title: 'עדכון', desc: 'ההתרעה טרם הסתיימה' },
      { cat: '1', title: 'האירוע הסתיים', desc: '' },
      { cat: '10', title: 'האירוע הסתיים', desc: 'היכנסו למרחב המוגן' },
      { title: 'התרעה כלשהי שאיננו מכירים', desc: '' },
      { cat: '10', title: 'האירוע הסתיים', desc: 'השוהים במרחב המוגן יכולים לצאת' },
      { cat: '10', title: 'בדקות הקרובות צפויות להתקבל התרעות באזורך', desc: '' },
    ].map(alertKind));
    console.log('classifier:', kinds.join(','));
    if (kinds.slice(0, 9).some(k => k !== 'attack')) fail('a message that may be an attack was classified as ' + kinds.slice(0, 9).join(','));
    if (kinds[9] !== 'ended' || kinds[10] !== 'early') fail('ended/early not recognised: ' + kinds.slice(9).join(','));

    // A second siren restarts the protected stay, and "you may leave" never shows while the alert is on.
    const stay = await page.evaluate(async () => {
      localStorage.setItem('dl-city', 'בת ים');
      const calls = []; const orig = window.startShelterCountdown;
      window.startShelterCountdown = a => { calls.push(1); return orig(a); };
      handleAlertData({ id: 'r1', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: '' });
      handleAlertData({ id: 'r1', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: '' });   // same siren, next poll
      const afterSame = calls.length;
      handleAlertData({ id: 'r2', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: '' });   // a new siren
      window.startShelterCountdown = orig;
      const out = { afterSame, afterNew: calls.length };
      handleAlertData([]);
      return out;
    });
    console.log('stay restart:', JSON.stringify(stay));
    if (!(stay.afterNew > stay.afterSame)) fail('a new siren does not restart the protected-stay countdown');

    // "the event has ended" and an early warning are not attacks: no siren, no takeover
    await page.evaluate(() => { window.__sirens = 0; window.playSiren = () => { window.__sirens++; }; });
    for (const [name, obj, wantTakeover] of [
        ['ended', { id: 'e1', cat: '10', title: 'האירוע הסתיים', data: ['בת ים'], desc: 'ניתן לצאת מהמרחב המוגן' }, false],
        ['early', { id: 'w1', cat: '10', title: 'בדקות הקרובות צפויות להתקבל התרעות באזורך', data: ['בת ים'], desc: '' }, false],
        ['attack', { id: 'a1', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: 'היכנסו למרחב המוגן' }, true]]) {
      const r = await page.evaluate(o => { window.__sirens = 0; handleAlertData(o);
        const n = document.getElementById('liveNotice');
        return { takeover: document.getElementById('takeover').classList.contains('show'), sirens: window.__sirens,
                 active: !!state.alertActive, notice: !!(n && !n.hidden) }; }, obj);
      console.log('alert-kind', name, JSON.stringify(r));
      if (r.takeover !== wantTakeover || r.active !== wantTakeover || (r.sirens > 0) !== wantTakeover) fail('live message "' + name + '" handled as ' + JSON.stringify(r));
      if (!wantTakeover && !r.notice) fail('live message "' + name + '" is not shown at all');
      await page.evaluate(() => { handleAlertData([]); const n = document.getElementById('liveNotice'); if (n) n.hidden = true; });
    }

    // an old answer must be labelled as old
    await page.evaluate(async () => { await tfetch('/quakes'); });
    await page.route('**/quakes', r => r.fulfill({ status: 200, contentType: 'application/json', body: '[]',
      headers: { 'X-DL-Stale': '1800' } }));
    await page.evaluate(async () => { await tfetch('/quakes'); });
    const bar = await page.evaluate(() => { const b = document.getElementById('staleBar'); return b && !b.hidden ? b.textContent : ''; });
    console.log('stale bar:', bar);
    if (!bar.includes('רעידות אדמה')) fail('stale data is not labelled');
    await page.unroute('**/quakes');
    await page.route('**/quakes', r => r.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
    await page.evaluate(async () => { await tfetch('/quakes'); });
    const cleared = await page.evaluate(() => { const b = document.getElementById('staleBar'); return !b || b.hidden || !b.textContent.includes('רעידות אדמה'); });
    if (!cleared) fail('the stale bar still names a source after it recovered');
    await page.unroute('**/quakes');

    // a screen that cannot write the file follows the owner's home until it chooses its own
    {
      const base = 'http://127.0.0.1:' + PORT;
      const cfg = await (await fetch(base + '/config')).json();
      const phone = await (await browser.newContext()).newPage();
      let city = 'חולון';
      await phone.route('**/config', async r => {
        if (r.request().method() !== 'GET') return r.fulfill({ status: 403, body: '{}' });
        const c = JSON.parse(JSON.stringify(cfg)); c.writable = false;
        c.config.prefs['dl-city'] = city; c.config.prefs['dl-lat'] = '32.011'; c.config.prefs['dl-lng'] = '34.772';
        r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(c) });
      });
      const cityOf = () => phone.evaluate(() => localStorage.getItem('dl-city'));
      await phone.goto(base + '/', { waitUntil: 'domcontentloaded' });
      const first = await cityOf();
      city = 'חיפה';                                   // the owner moves the home on the PC
      await phone.reload({ waitUntil: 'domcontentloaded' });
      const followed = await cityOf();
      await phone.evaluate(() => localStorage.setItem('dl-city', 'אילת'));     // the phone picks its own city
      city = 'רחובות';
      await phone.reload({ waitUntil: 'domcontentloaded' });
      const own = await cityOf();
      console.log('read-only screen:', first, '->', followed, '-> own choice kept:', own);
      if (first !== 'חולון' || followed !== 'חיפה') fail('a read-only screen does not follow the owner (' + first + ', ' + followed + ')');
      if (own !== 'אילת') fail('a read-only screen lost its own choice (' + own + ')');
      await phone.context().close();
    }

    // saving one change must not disturb stored ids the screen does not show
    await fetch('http://127.0.0.1:' + PORT + '/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patch: { prefs: { 'dl-news-off': '["tg_hidden_source"]', 'dl-lat': '31.5', 'dl-lng': '34.9' } } }) });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    await page.click('#settingsBtn');
    await page.waitForSelector('#setHeavy');
    await page.fill('#setHeavy', '33');
    await page.click('#setSave');
    await page.waitForTimeout(4000);
    const kept = (await (await fetch('http://127.0.0.1:' + PORT + '/config')).json()).config;
    console.log('after a one-field save:', kept.prefs['dl-news-off'], kept.prefs['dl-lat'], kept.traffic.heavyDelay);
    if (!String(kept.prefs['dl-news-off']).includes('tg_hidden_source')) fail('a hidden source id was dropped by a settings save');
    if (kept.prefs['dl-lat'] !== '31.5' || kept.traffic.heavyDelay !== 33) fail('a one-field save changed other settings');

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
