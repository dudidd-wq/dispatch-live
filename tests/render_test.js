// Starts the real server and checks the deck in a headless browser:
// both maps exist with history, without history, and when Oref is down.
const { spawn } = require('child_process');
const path = require('path');
const { chromium } = require('playwright');
const fs = require('fs'), os = require('os');
const SRC = path.join(__dirname, '..');
// a scratch copy: the settings test writes config.local.json and must not touch the checkout
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-render-'));
for (const f of ['index.html', 'server.py', 'brain.py', 'dlconfig.py', 'dlarchive.py', 'dlupdate.py']) fs.copyFileSync(path.join(SRC, f), path.join(ROOT, f));
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

    // Counting oscillators is not hearing them: check the audio context really
    // runs when the browser allows sound (the deck's launch flags), and that the
    // deck SAYS so when the browser blocks it.
    for (const [policy, wantRunning] of [['no-user-gesture-required', true], ['user-gesture-required', false]]) {
      const b = await chromium.launch({ args: ['--autoplay-policy=' + policy] });
      try {
        const pg = await (await b.newContext()).newPage();
        await pg.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'domcontentloaded' });
        await pg.waitForTimeout(1500);
        await pg.evaluate(() => { localStorage.setItem('dl-city', 'בת ים');
          handleAlertData({ id: 'au1', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: '' }); });
        await pg.waitForTimeout(600);
        const au = await pg.evaluate(() => { renderAudioBlocked(); const el = document.getElementById('audioBlocked');
          return { state: alertAudio && alertAudio.state, badge: !!(el && !el.hidden) }; });
        console.log('audio with --autoplay-policy=' + policy + ':', JSON.stringify(au));
        if (wantRunning && (au.state !== 'running' || au.badge)) fail('sound allowed but the siren is not playing: ' + JSON.stringify(au));
        if (!wantRunning && au.state !== 'running' && !au.badge) fail('sound blocked and the deck does not say so');
        if (!wantRunning && au.state === 'running') {
          // headless Chromium plays sound even so: show the deck a blocked context instead
          const sim = await pg.evaluate(() => { const real = alertAudio;
            alertAudio = { state: 'suspended', resume: () => Promise.resolve(), currentTime: 0 };
            renderAudioBlocked(); const el = document.getElementById('audioBlocked'); const shown = !!(el && !el.hidden);
            alertAudio = real; renderAudioBlocked(); const gone = !el || el.hidden; return { shown, gone }; });
          console.log('  blocked sound (simulated):', JSON.stringify(sim));
          if (!sim.shown || !sim.gone) fail('the "sound is blocked" badge does not follow the audio state: ' + JSON.stringify(sim));
        }
      } finally { await b.close(); }
    }

    // Every message in Oref's own table is classified by tests/check_alertkind.js.
    // Here: real-alert categories and unknown messages stay attacks whatever the body says.
    const kinds = await page.evaluate(() => [
      { cat: '1', title: 'ירי רקטות וטילים', desc: 'היכנסו למרחב המוגן ושהו בו 10 דקות, לאחר מכן ניתן לצאת' },
      { cat: '3', title: 'רעידת אדמה', desc: 'אם לא ניתן לצאת מהמבנה היכנסו לממ"ד' },
      { cat: '7', title: 'אירוע חומרים מסוכנים', desc: 'הישארו בבתים עד להודעה כי ניתן לצאת' },
      { cat: '13', title: 'חדירת מחבלים', desc: 'אינכם יכולים לצאת' },
      { cat: '6', title: 'חדירת כלי טיס עוין', desc: 'בדקות הקרובות ייתכנו התרעות נוספות' },
      { cat: '1', title: 'האירוע הסתיים', desc: '' },
      { title: 'התרעה כלשהי שאיננו מכירים', desc: '' },
      { cat: '10', title: 'האירוע הסתיים', desc: 'השוהים במרחב המוגן יכולים לצאת' },
      { cat: '10', title: 'בדקות הקרובות צפויות להתקבל התרעות באזורך', desc: '' },
      { cat: '10', title: 'היכנסו מיד למרחב המוגן', desc: '' },
      { cat: '10', title: 'פיקוד העורף מעדכן', desc: 'יש להמשיך לשהות במרחב המוגן עד להודעה חדשה' },
    ].map(alertKind));
    console.log('classifier:', kinds.join(','));
    if (kinds.slice(0, 7).some(k => k !== 'attack')) fail('a message that may be an attack was classified as ' + kinds.slice(0, 7).join(','));
    if (kinds.slice(7).join() !== 'ended,early,shelter,shelter') fail('cat-10 kinds wrong: ' + kinds.slice(7).join(','));

    // Between scenarios: no alert, no stay, nothing remembered.
    const reset = () => page.evaluate(() => {
      handleAlertData([]); document.getElementById('takeoverDismiss').click();
      state.lastAlertId = null; state.chimed.clear(); hideLiveNotice(); window.__sirens = 0; window.__chimes = 0;
      localStorage.setItem('dl-city', 'בת ים');
      geo.lat = 32.017; geo.lng = 34.745; geo.city = 'בת ים';     // home in the centre: Kiryat Shmona is elsewhere
    });
    const view = () => page.evaluate(() => ({
      takeover: document.getElementById('takeover').classList.contains('show'),
      ended: document.getElementById('takeover').classList.contains('ended'),
      mine: document.getElementById('shelterCard').classList.contains('mine'),
      scope: document.getElementById('scScope').textContent, phase: document.getElementById('scPhase').textContent,
      count: document.getElementById('scCount').textContent,
      stay: !!state.stay, further: !!state.stay && state.stay.stayEnd === null, saved: !!localStorage.getItem('dl-stay'),
      active: !!state.alertActive, sirens: window.__sirens, chimes: window.__chimes,
      notice: !!(document.getElementById('liveNotice') && !document.getElementById('liveNotice').hidden) }));
    await page.evaluate(() => { window.__sirens = 0; window.__chimes = 0;
      window.playSiren = () => { window.__sirens++; }; window.playChime = () => { window.__chimes++; }; });
    const send = o => page.evaluate(o => handleAlertData(o), o);
    const ATK = (id, towns) => ({ id, cat: '1', title: 'ירי רקטות וטילים', data: towns, desc: 'היכנסו למרחב המוגן' });

    // A second siren restarts the protected stay; the same siren polled again does not.
    await reset();
    const stay = await page.evaluate(() => {
      const calls = []; const orig = window.startShelterCountdown;
      window.startShelterCountdown = (...a) => { calls.push(1); return orig(...a); };
      const A = id => ({ id, cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: '' });
      handleAlertData(A('r1')); handleAlertData(A('r1'));
      const afterSame = calls.length;
      handleAlertData(A('r2'));
      window.startShelterCountdown = orig;
      return { afterSame, afterNew: calls.length };
    });
    console.log('stay restart:', JSON.stringify(stay));
    if (stay.afterSame !== 1 || stay.afterNew !== 2) fail('a new siren does not restart the protected-stay countdown');

    // The four kinds of live message.
    for (const [name, obj, want] of [
        ['ended', { id: 'e1', cat: '10', title: 'האירוע הסתיים', data: ['בת ים'], desc: 'ניתן לצאת מהמרחב המוגן' }, { takeover: false, sirens: 0, notice: true }],
        ['early', { id: 'w1', cat: '10', title: 'בדקות הקרובות צפויות להתקבל התרעות באזורך', data: ['בת ים'], desc: '' }, { takeover: false, sirens: 0, notice: true, chimes: 1 }],
        ['attack', ATK('a1', ['בת ים']), { takeover: true, sirens: 1, stay: true, further: false }],
        ['shelter', { id: 'h1', cat: '10', title: 'חדירת מחבלים - אין לצאת מהמרחב המוגן', data: ['בת ים'], desc: '' }, { takeover: true, sirens: 1, stay: true, further: true }]]) {
      await reset();
      await page.evaluate(() => { window.__sirens = 0; window.__chimes = 0; });
      await send(obj);
      const r = await view();
      console.log('alert-kind', name, JSON.stringify({ takeover: r.takeover, sirens: r.sirens, chimes: r.chimes, stay: r.stay, further: r.further, notice: r.notice }));
      for (const [k, v] of Object.entries(want)) if (r[k] !== v) fail('live message "' + name + '": ' + k + '=' + r[k] + ', expected ' + v);
      if (name === 'shelter' && !r.phase.includes('אין לצאת')) fail('a stay-until-further-notice does not show Oref\'s instruction: ' + r.phase);
    }
    // ... and it does not end by the clock: past the 10 minutes it still says stay
    await page.evaluate(() => { state.stay.p1End = Date.now() - 11 * 60e3; state.stay.at = Date.now() - 12 * 60e3; });
    await send([]);
    await page.waitForTimeout(1200);
    { const r = await view();
      console.log('further notice after 12 min:', r.count, r.phase);
      if (!r.stay || !r.takeover || r.ended || /ניתן לצאת/.test(r.phase)) fail('a stay-until-further-notice ended by the clock'); }
    await send({ id: 'h2', cat: '10', title: 'חדירת מחבלים - החשש הוסר', data: ['בת ים'], desc: '' });
    { const r = await view();
      if (r.stay || !r.ended || r.saved) fail('Oref\'s all-clear did not end the stay: ' + JSON.stringify(r)); }

    // S1: an alert somewhere else, in phase 1 and in phase 2 of MY stay, leaves my stay alone
    await reset();
    await send(ATK('m1', ['בת ים']));
    await send(ATK('o1', ['קריית שמונה']));
    let r1 = await view();
    await page.evaluate(() => { state.stay.p1End = Date.now() - 1000; });
    await page.waitForTimeout(1200);
    await send(ATK('o2', ['קריית שמונה']));
    let r2 = await view();
    await send([]);
    await send({ id: 'oe', cat: '10', title: 'האירוע הסתיים', data: ['קריית שמונה'], desc: '' });
    let r3 = await view();
    console.log('other town during my stay:', JSON.stringify([r1.scope, r1.count, r2.scope, r2.count, r3.stay]));
    if (!r1.stay || !r1.mine || !/ממשיכים/.test(r1.scope) || !/^\d+$/.test(r1.count)) fail('an alert elsewhere disturbed phase 1 of my stay: ' + JSON.stringify(r1));
    if (!r2.stay || !r2.mine || !/ממשיכים/.test(r2.scope) || !/:/.test(r2.count)) fail('an alert elsewhere disturbed phase 2 of my stay: ' + JSON.stringify(r2));
    if (!r3.stay || r3.ended) fail('an all-clear for another town ended my stay');
    if (r2.sirens !== 3) fail('expected a siren per new alert (3), got ' + r2.sirens);

    // an all-clear for MY town that came in one list with a siren elsewhere ends my stay
    await reset();
    await send(ATK('m2', ['בת ים']));
    await send({ id: 'm2+o3', cat: '1', title: 'ירי רקטות וטילים', data: ['קריית שמונה'], desc: '', ended: ['בת ים'] });
    { const r = await view(); console.log('merged all-clear for my town:', r.stay, r.mine);
      if (r.stay) fail('an all-clear for my town merged with a siren elsewhere did not end my stay'); }

    // S2: my town added to an alert already on screen (same id, longer list)
    await reset();
    await page.evaluate(() => { window.__sirens = 0; });
    await send(ATK('g1', ['קריית שמונה']));
    const g1 = await view();
    await send(ATK('g1', ['קריית שמונה', 'בת ים']));
    const g2 = await view();
    await send(ATK('g1', ['קריית שמונה', 'בת ים']));
    const g3 = await view();
    console.log('my town added to a running alert:', g1.sirens, g1.stay, '->', g2.sirens, g2.stay, '->', g3.sirens);
    if (g1.stay || g2.sirens !== 2 || !g2.stay || g3.sirens !== 2) fail('my town added to a running alert: ' + JSON.stringify([g1.sirens, g1.stay, g2.sirens, g2.stay, g3.sirens]));

    // a heads-up for my area chimes once per message, not once per poll
    await reset();
    await page.evaluate(() => { window.__chimes = 0; });
    const EARLY = id => ({ id, cat: '10', title: 'בדקות הקרובות צפויות להתקבל התרעות באזורך', data: ['בת ים'], desc: '' });
    for (let i = 0; i < 4; i++) await send(EARLY('c1'));
    await send(EARLY('c2'));
    await send({ id: 'c3', cat: '10', title: 'בדקות הקרובות צפויות להתקבל התרעות באזורך', data: ['קריית שמונה'], desc: '' });
    const ch = await view();
    console.log('chimes for c1 x4, c2, c3 elsewhere:', ch.chimes);
    if (ch.chimes !== 2) fail('expected 2 chimes, got ' + ch.chimes);
    await reset();

    // S5: a reload (or crash recovery) in the middle of my stay resumes it
    {
      const ctx = await browser.newContext(); const pg = await ctx.newPage();
      await pg.goto('http://127.0.0.1:' + PORT + '/', { waitUntil: 'domcontentloaded' });
      await pg.waitForTimeout(1500);
      await pg.evaluate(() => { localStorage.setItem('dl-city', 'בת ים'); window.playSiren = () => {};
        handleAlertData({ id: 'x1', cat: '1', title: 'ירי רקטות וטילים', data: ['בת ים'], desc: '' });
        state.stay.p1End = Date.now() - 2 * 60e3; state.stay.stayEnd = Date.now() + 8 * 60e3; saveStay(); });
      await pg.reload({ waitUntil: 'domcontentloaded' });
      await pg.waitForTimeout(2500);
      const rs = await pg.evaluate(() => ({ running: stayRunning(), takeover: document.getElementById('takeover').classList.contains('show'),
        count: document.getElementById('scCount').textContent }));
      console.log('stay after a reload:', JSON.stringify(rs));
      if (!rs.running || !rs.takeover || !/^[78]:\d\d$/.test(rs.count)) fail('a reload dropped the protected stay: ' + JSON.stringify(rs));
      await ctx.close();
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
      await phone.evaluate(() => { localStorage.setItem('dl-lat', '29.557'); localStorage.setItem('dl-lng', '34.952');
        localStorage.setItem('dl-city', 'אילת'); });     // the phone picks its own city (the picker sets all three)
      city = 'רחובות';
      await phone.reload({ waitUntil: 'domcontentloaded' });
      const own = await cityOf();
      console.log('read-only screen:', first, '->', followed, '-> own choice kept:', own);
      if (first !== 'חולון' || followed !== 'חיפה') fail('a read-only screen does not follow the owner (' + first + ', ' + followed + ')');
      if (own !== 'אילת') fail('a read-only screen lost its own choice (' + own + ')');
      // its own city carries its own coordinates: the home is one setting
      const coords = await phone.evaluate(() => localStorage.getItem('dl-lat'));
      if (coords !== '29.557') fail('a read-only screen with its own city lost its own coordinates: ' + coords);
      await phone.context().close();

      // "automatic location" chosen on the phone is a choice, not a gap to fill
      const p3 = await (await browser.newContext()).newPage();
      await p3.route('**/config', async r => {
        const c = JSON.parse(JSON.stringify(cfg)); c.writable = false;
        c.config.prefs['dl-city'] = 'חיפה'; c.config.prefs['dl-lat'] = '32.794'; c.config.prefs['dl-lng'] = '34.989';
        r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(c) });
      });
      await p3.goto(base + '/', { waitUntil: 'domcontentloaded' });
      await p3.evaluate(() => { localStorage.removeItem('dl-city'); localStorage.removeItem('dl-lat'); localStorage.removeItem('dl-lng'); });
      await p3.reload({ waitUntil: 'domcontentloaded' });
      const auto = await p3.evaluate(() => [localStorage.getItem('dl-city'), localStorage.getItem('dl-lat')]);
      if (auto[0] !== null || auto[1] !== null) fail('a phone set to automatic location got the owner\'s home back: ' + auto);
      await p3.context().close();

      // a device that already had its own city before it ever saw the file keeps it
      const p4 = await (await browser.newContext()).newPage();
      await p4.addInitScript(() => { if (!localStorage.getItem('__seeded')) {
        localStorage.setItem('__seeded', '1'); localStorage.setItem('dl-city', 'באר שבע');
        localStorage.setItem('dl-lat', '31.252'); localStorage.setItem('dl-lng', '34.791'); } });
      await p4.route('**/config', async r => {
        const c = JSON.parse(JSON.stringify(cfg)); c.writable = false;
        c.config.prefs['dl-city'] = 'חיפה'; c.config.prefs['dl-lat'] = '32.794'; c.config.prefs['dl-lng'] = '34.989';
        r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(c) });
      });
      await p4.goto(base + '/', { waitUntil: 'domcontentloaded' });
      const pre = await p4.evaluate(() => [localStorage.getItem('dl-city'), localStorage.getItem('dl-lat')]);
      console.log('pre-existing own home kept:', pre.join(' '));
      if (pre[0] !== 'באר שבע' || pre[1] !== '31.252') fail('the first load overwrote a device\'s own home: ' + pre);
      await p4.context().close();
    }

    // display settings: text size for the whole screen, news columns, opening focus, the strip
    {
      const base = 'http://127.0.0.1:' + PORT;
      const cfg = await (await fetch(base + '/config')).json();
      const pg = await (await browser.newContext({ viewport: { width: 1920, height: 1080 } })).newPage();
      await pg.route('**/config', async r => {
        if (r.request().method() !== 'GET') return r.continue();
        const c = JSON.parse(JSON.stringify(cfg));
        c.config.display = { fontScale: 1, uiScale: 1.2, newsCols: 1, startFocus: 'alerts', lifeStrip: 'not-tv', theme: 'mono' };
        r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(c) });
      });
      await pg.goto(base + '/', { waitUntil: 'domcontentloaded' });
      await pg.waitForTimeout(3000);
      const d = await pg.evaluate(() => ({ title: parseFloat(getComputedStyle(document.querySelector('.block-title')).fontSize),
        focus: state.focus, cols: getComputedStyle(document.querySelector('.news-rows-wrap') || document.body).columnCount,
        strip: getComputedStyle(document.getElementById('lifeStrip')).display,
        theme: document.body.className, bg: getComputedStyle(document.querySelector('.block')).backgroundColor }));
      await pg.evaluate(() => document.querySelector('.focus-opt[data-focus="tv"]').click());
      await pg.waitForTimeout(500);
      const stripTv = await pg.evaluate(() => getComputedStyle(document.getElementById('lifeStrip')).display);
      console.log('display settings:', JSON.stringify(d), 'strip in TV focus:', stripTv);
      if (Math.abs(d.title - 14 * 1.2) > 0.05) fail('text size for the whole screen not applied: ' + d.title);
      if (d.focus !== 'alerts') fail('opening focus not applied: ' + d.focus);
      if (!/theme-mono/.test(d.theme) || d.bg !== 'rgb(10, 10, 10)') fail('theme not applied: ' + d.theme + ' ' + d.bg);
      if (d.cols !== '1') fail('news columns not applied: ' + d.cols);
      if (d.strip === 'none' || stripTv !== 'none') fail('markets strip rule not applied: ' + d.strip + '/' + stripTv);
      await pg.context().close();
    }

    // Shabbat band helpers
    {
      const h = await page.evaluate(() => [hdateHe('11 Kislev 5787'), hdateHe('15 Sh\'vat 5787'), hdateHe('1 Adar II 5787'),
        haftaraHe('Hosea 12:13-14:10'), haftaraHe('I Kings 18:46-19:21'), heDay(30), heDay(16)]);
      console.log('shabbat band helpers:', h.join(' | '));
      if (h.join('|') !== 'י״א כסלו|ט״ו שבט|א׳ אדר ב׳|הושע 12:13–14:10|מלכים א 18:46–19:21|ל׳|ט״ז') fail('Hebrew date or Haftarah conversion: ' + h.join('|'));
    }

    // pairing custom entries across a save
    const pairs = await page.evaluate(() => {
      const ch = (id, name, src) => ({ id, name, src });
      return [
        pairEdited([ch('a', 'Cam', 'u1'), ch('b', 'Cam', 'u2')], [ch('a', 'Cam', 'u1'), ch('b', 'Cam', 'u2')], 'src'),   // nothing edited
        pairEdited([ch('a', 'Cam', 'u1'), ch('b', 'Cam', 'u2')], [ch('a', 'Cam', 'u1'), ch('c', 'Cam', 'u3')], 'src'),   // one address edited
        pairEdited([ch('a', 'A', 'u1')], [ch('x', 'A', 'u9'), ch('y', 'B', 'u8')], 'src'),                              // edit + add
        pairEdited([ch('a', 'A', 'u1'), ch('b', 'B', 'u2')], [ch('x', 'C', 'u7'), ch('y', 'D', 'u8')], 'src'),           // ambiguous
      ];
    });
    console.log('pairing:', JSON.stringify(pairs));
    if (Object.keys(pairs[0].map).length || pairs[0].added.length) fail('unedited entries were re-mapped');
    if (pairs[1].map.b !== 'c' || pairs[1].map.a) fail('a single address edit was not carried over');
    if (pairs[2].map.a !== 'x' || pairs[2].added.join() !== 'y') fail('edit plus add mis-paired');
    if (Object.keys(pairs[3].map).length) fail('an ambiguous edit was guessed');

    // a message without an id still sounds again after an all-clear or a quiet spell
    await reset();
    const resound = await page.evaluate(() => {
      let n = 0; const orig = window.playSiren; window.playSiren = () => { n++; };
      const atk = { cat: '1', title: 'ירי רקטות וטילים', data: ['שדרות'], desc: '' };
      handleAlertData(atk); handleAlertData(atk);                                  // one siren, polled twice
      handleAlertData({ cat: '10', title: 'האירוע הסתיים', data: ['שדרות'], desc: '' });
      handleAlertData(atk);                                                        // a new attack, same towns
      handleAlertData([]); handleAlertData(atk);                                   // quiet, then again
      window.playSiren = orig; handleAlertData([]);
      return n;
    });
    console.log('sirens for attack, attack(repeat), ended, attack, quiet, attack:', resound);
    if (resound !== 3) fail('expected 3 sirens without ids, got ' + resound);

    // no self-reload during an alert or a protected stay
    const reloadGuard = await page.evaluate(() => {
      const src = String(maybeFullReload);
      return /alertActive/.test(src) && /stayRunning\(\)|_shelterTimer/.test(src) && !/HARD/.test(src);
    });
    if (!reloadGuard) fail('the periodic reload is not held back during an alert');

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
