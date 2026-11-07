// اختبار واجهة فلتر «النفط والطاقة» في تبويب الأخبار عند 390 و1280 بكسل. يُشغَّل محليًا (يحتاج Node وPlaywright):
//   node tests/news_ui.js
// يخدم المستودع على منفذ محلي ويستبدل ملفي الأخبار بعناوين اختبار، ولا يكتب في المستودع شيئًا.
// يثبت: لا أخطاء JS، ولا تمرير أفقي، وثلاثة أزرار بأعداد صحيحة، وكل فلتر يعرض عناوينه فقط، وشارة «نفط» في «الكل».
const {chromium} = require('playwright');
const {spawn} = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PORT = 8772;
const now = Date.now(), iso = m => new Date(now - m * 60000).toISOString();
const MAC = {updated: iso(1), items: [
  {id: 1, utc: iso(3), title_en: 'Brent crude jumps 4% after tanker attack near Hormuz', title_ar: 'خام برنت يقفز 4%', labels: []},
  {id: 2, utc: iso(5), title_en: 'Gold climbs as Treasury yields fall', title_ar: 'الذهب يرتفع', labels: []},
  {id: 3, utc: iso(7), title_en: 'Apple unveils new phone', title_ar: 'أبل تكشف هاتفًا', labels: []},
  {id: 4, utc: iso(9), title_en: 'OPEC+ agrees deeper output cut, Iran warns', title_ar: 'أوبك+ تعمّق الخفض', labels: []},
  {id: 5, utc: iso(11), title_en: 'Soybean oilseed exports rise', title_ar: 'صادرات البذور الزيتية', labels: []}]};
const fails = [];
const check = (ok, msg) => { console.log((ok ? 'PASS ' : 'FAIL ') + msg); if (!ok) fails.push(msg); };

(async () => {
  const srv = spawn('python3', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1', '--directory', ROOT], {stdio: 'ignore'});
  await new Promise(r => setTimeout(r, 800));
  const b = await chromium.launch();
  try {
    for (const width of [390, 1280]) {
      const p = await b.newPage({viewport: {width, height: 900}});
      const errs = []; p.on('pageerror', e => errs.push(e.message));
      await p.route('**/*news.json*', r => r.request().url().includes('/macro-platform/')
        ? r.fulfill({body: JSON.stringify(MAC), contentType: 'application/json'}) : r.fulfill({status: 404, body: ''}));
      await p.goto(`http://127.0.0.1:${PORT}/index.html?weekend=0#news`);
      await p.waitForSelector('section.news .chips', {timeout: 15000}).catch(() => {});
      const read = () => p.evaluate(() => ({
        chips: [...document.querySelectorAll('section.news [data-news-f]')].map(x => [x.dataset.newsF, x.innerText.trim(), x.getAttribute('aria-pressed')]),
        rows: [...document.querySelectorAll('section.news .nw-row .nw-t')].map(x => x.innerText.trim()),
        oilTags: document.querySelectorAll('section.news .nw-tag.oil').length,
        over: document.documentElement.scrollWidth - innerWidth}));
      const tag = `@${width}`;
      let r = await read();
      check(r.chips.length === 3, `${tag}: three filter chips (${r.chips.map(c => c[1]).join(' | ')})`);
      const count = f => Number(((r.chips.find(c => c[0] === f) || [])[1] || '').replace(/[^\d]/g, ''));
      // الذهب 3: عنوان برنت يذكر attack وعنوان أوبك يذكر Iran، فيلتقطهما فلتر الذهب القائم أيضًا (سلوكه لم يتغير)
      check(count('all') === 5 && count('gold') === 3 && count('oil') === 2, `${tag}: counts all 5, gold 3, oil 2`);
      check(r.rows.length === 5 && r.oilTags === 2, `${tag}: "all" shows 5 rows with 2 oil tags`);
      check(r.over <= 0, `${tag}: no horizontal overflow (${r.over})`);
      await p.click('section.news [data-news-f="oil"]');
      r = await read();
      check(r.rows.length === 2 && r.rows.every(t => /برنت|أوبك/.test(t)), `${tag}: oil filter shows only the 2 oil headlines (${r.rows.join(' | ')})`);
      check(r.chips.find(c => c[0] === 'oil')[2] === 'true' && r.oilTags === 0, `${tag}: oil chip pressed, no redundant tag`);
      check(r.over <= 0, `${tag}: no horizontal overflow on the oil filter (${r.over})`);
      await p.click('section.news [data-news-f="gold"]');
      r = await read();
      check(r.rows.length === 3 && r.rows.some(t => t.includes('الذهب')) && !r.rows.some(t => /أبل|البذور/.test(t)), `${tag}: gold filter unchanged (${r.rows.join(' | ')})`);
      check(errs.length === 0, `${tag}: no JS errors ${errs.length ? errs[0] : ''}`);
      if (width === 390) await p.screenshot({path: path.join(process.env.SHOT_DIR || '/tmp', 'news_oil_390.png'), fullPage: false});
      await p.close();
    }
  } finally {
    await b.close();
    srv.kill();
  }
  console.log(`\n${fails.length} failure(s)`);
  process.exit(fails.length ? 1 : 0);
})();
