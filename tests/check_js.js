// Every inline <script> in index.html must at least parse.
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const re = /<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g;
let m, n = 0;
while ((m = re.exec(html))) {
  n++;
  try { new Function(m[1]); } catch (e) { console.error('inline script #' + n + ': ' + e.message); process.exit(1); }
}
if (!n) { console.error('no inline script found'); process.exit(1); }
console.log(n + ' inline script(s) parse OK');
