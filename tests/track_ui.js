// اختبار الواجهة لـ «سجل أداء الانحياز» (المادة 20) عند 390 و1280 بكسل. يُشغَّل محليًا (يحتاج Node وPlaywright):
//   python tests/test_track.py --fixtures /tmp/fx && node tests/track_ui.js /tmp/fx
// يخدم المستودع على منفذ محلي، ويستبدل data.json بكل ملف من ملفات الاختبار، ولا يكتب في المستودع شيئًا.
// يثبت: لا أخطاء JS، ولا تمرير أفقي، ولا تلوين للأرقام، ولا «تخمين» ولا 33٪ ولا 0.667، ولا متوسطات قبل 13 نافذة،
// والمقارنة بالمعدل التاريخي فقط بعد 13 نافذة لها معدل تاريخي، و«لا حكم بالمهارة» قبلها حتى لو اكتملت النوافذ.
const {chromium} = require('playwright');
const {spawn} = require('child_process');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const FX = path.resolve(process.argv[2] || '');
const PORT = 8771;
const GATED = ['real', 'n10', 'old'];
const fails = [];
const check = (ok, msg) => { console.log((ok ? 'PASS ' : 'FAIL ') + msg); if (!ok) fails.push(msg); };

(async () => {
  if (!fs.existsSync(path.join(FX, 'data_real.json'))) { console.error('run: python tests/test_track.py --fixtures DIR first'); process.exit(2); }
  const srv = spawn('python3', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1', '--directory', ROOT], {stdio: 'ignore'});
  await new Promise(r => setTimeout(r, 800));
  const b = await chromium.launch();
  try {
    for (const name of ['real', 'n10', 'old', 'prelim', 'final', 'nobase', 'fewbase']) {
      for (const width of [390, 1280]) {
        const p = await b.newPage({viewport: {width, height: 900}});
        const errs = []; p.on('pageerror', e => errs.push(e.message));
        await p.route('**/data.json*', r => r.fulfill({path: path.join(FX, `data_${name}.json`), contentType: 'application/json'}));
        await p.goto(`http://127.0.0.1:${PORT}/index.html?weekend=0#ai`);
        await p.waitForSelector('section.track', {timeout: 10000}).catch(() => {});
        const r = await p.evaluate(() => {
          const t = document.querySelector('section.track');
          if (!t) return null;
          const figs = [...t.querySelectorAll('.figs .fig')];
          const c = t.cloneNode(true); c.querySelectorAll('.tablebox').forEach(x => x.remove());   // the per-forecast table holds real probabilities
          return {text: t.innerText, outside: c.innerText, heads: figs.map(f => f.querySelector('h3').innerText),
                  vals: figs.map(f => f.querySelector('.v').innerText),
                  coloured: figs.filter(f => /\b(up|down)\b/.test(f.querySelector('.v').className)).length,
                  over: document.documentElement.scrollWidth - innerWidth};
        });
        const tag = `${name} @${width}`;
        check(r !== null, `${tag}: track section rendered`);
        if (!r) { await p.close(); continue; }
        check(errs.length === 0, `${tag}: no JS errors ${errs.length ? errs[0] : ''}`);
        check(r.over <= 0, `${tag}: no horizontal overflow (${r.over})`);
        check(r.coloured === 0, `${tag}: no coloured figure`);
        check(!/التخمين|≈\s*33|العملة|0\.667/.test(r.outside), `${tag}: no "guess", ≈33%, coin or 0.667 baseline outside the table`);
        if (GATED.includes(name)) {
          check(!r.heads.some(h => /أصاب|Brier|احتمال النتيجة|الميل/.test(h)), `${tag}: no averages below 13 windows (${r.heads.join(' | ')})`);
          check(!r.vals.some(v => v.includes('%')), `${tag}: no percentage shown (${r.vals.join(' | ')})`);
          check(r.text.includes('عيّنة غير كافية'), `${tag}: says the sample is not enough`);
        } else if (name === 'nobase' || name === 'fewbase') {
          check(r.heads.includes('متوسط Brier'), `${tag}: descriptive averages shown after 13 windows`);
          check(!/المعدل التاريخي:\s*\d/.test(r.outside) && r.outside.includes('لا مقارنة بالمعدل التاريخي'), `${tag}: no comparison with the base rate`);
          check(r.outside.includes('لا حكم بالمهارة بعد') && !/مؤشر أولي|عتبة الحكم النهائي/.test(r.outside), `${tag}: says there is no skill verdict yet`);
        } else {
          check(r.heads.includes('متوسط Brier') && /المعدل التاريخي:\s*\d/.test(r.outside), `${tag}: Brier shown against the historical base rate`);
          check(r.text.includes(name === 'prelim' ? 'مؤشر أولي' : 'عتبة الحكم النهائي'), `${tag}: labelled ${name === 'prelim' ? 'preliminary' : 'final threshold'}`);
        }
        await p.close();
      }
    }
  } finally {
    await b.close();
    srv.kill();
  }
  console.log(`\n${fails.length} failure(s)`);
  process.exit(fails.length ? 1 : 0);
})();
