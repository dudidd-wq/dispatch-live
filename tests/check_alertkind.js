// Classifies every message in Oref's own message table with the page's
// alertKind() and compares with tests/fixtures/oref_expected.json.
// No browser needed: the classifier is pulled out of index.html.
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const grab = re => { const m = html.match(re); if (!m) { console.error('not found: ' + re); process.exit(1); } return m[0]; };
const code = [grab(/const OREF_SHELTER = .*;/), grab(/const OREF_ENDED = .*;/),
              grab(/function alertKind\(data\) \{[\s\S]*?\n\}/)].join('\n');
const alertKind = new Function(code + '\nreturn alertKind;')();
const norm = t => String(t).replace(/\s+/g, ' ').trim();
const table = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'oref_titles.json'), 'utf8')).entries;
const expected = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'oref_expected.json'), 'utf8'));
const want = {}; for (const [k, v] of Object.entries(expected)) if (!k.startsWith('_')) want[norm(k)] = v;
let bad = 0, n = 0;
for (const e of table) {
  const msg = { cat: String(e.matrixCatId), title: e.hebTitle, desc: e.heb };
  const got = alertKind(msg);
  const exp = e.matrixCatId === 10 ? want[norm(e.hebTitle)] : 'attack';
  n++;
  if (!exp) { console.error('no expectation for cat-10 title: ' + e.hebTitle); bad++; continue; }
  if (got !== exp) { console.error(`WRONG  cat ${e.matrixCatId} "${e.hebTitle}": got ${got}, expected ${exp}`); bad++; }
}
// The attack messages with no category at all must still sound the siren (attack or shelter)
for (const e of table.filter(e => e.matrixCatId !== 10)) {
  const k = alertKind({ title: e.hebTitle, desc: e.heb });
  if (k !== 'attack' && k !== 'shelter') { console.error('no-cat attack read as silent (' + k + '): ' + e.hebTitle); bad++; }
}
// Hand-made traps: an attack whose instructions mention leaving, and unknown wording
for (const m of [{ cat: '1', title: 'ירי רקטות וטילים', desc: 'היכנסו למרחב המוגן ושהו בו 10 דקות, לאחר מכן ניתן לצאת' },
                 { cat: '6', title: 'חדירת כלי טיס עוין', desc: 'בדקות הקרובות ייתכנו התרעות נוספות' },
                 { cat: '13', title: 'האירוע הסתיים', desc: '' },
                 { title: 'התרעה כלשהי שאיננו מכירים', desc: '' }]) {
  if (alertKind(m) !== 'attack') { console.error('trap read as non-attack: ' + JSON.stringify(m)); bad++; }
}
// The live feed's desc need not match the table: the headline alone must decide
for (const [m, want] of [
    [{ cat: '10', title: 'היכנסו מיד למרחב המוגן', desc: '' }, 'shelter'],
    [{ cat: '10', title: 'חדירת מחבלים - אין לצאת מהמרחב המוגן', desc: '' }, 'shelter'],
    [{ cat: '10', title: 'יש להמשיך לשהות במרחב המוגן', desc: 'בסיום האירוע ניתן יהיה לצאת' }, 'shelter'],
    [{ cat: '10', title: 'ניתן לצאת מהמרחב המוגן', desc: 'בהתרעה הבאה היכנסו למרחב המוגן' }, 'ended'],
    [{ cat: '10', title: 'האירוע הסתיים', desc: '' }, 'ended'],
    [{ cat: '10', title: 'פיקוד העורף מעדכן', desc: 'יש להמשיך לשהות במרחב המוגן עד להודעה חדשה' }, 'shelter'],
    [{ cat: '10', title: 'פיקוד העורף מעדכן', desc: '' }, 'early']]) {
  n++;
  const got = alertKind(m);
  if (got !== want) { console.error(`WRONG  "${m.title}" / "${m.desc}": got ${got}, expected ${want}`); bad++; }
}
console.log(`${n} Oref messages classified, ${bad} wrong`);
process.exit(bad ? 1 : 0);
