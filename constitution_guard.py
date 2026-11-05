"""حارس الدستور: كود يحكم الدستور بدل الثقة بالذاكرة.

1) كل رقم أو اسم في CONSTITUTION.md وREADME.md يتغير مع الكود أو البيانات مكتوب بين علامتين
   <!--a:المفتاح-->القيمة<!--/a--> ، والحارس يحسب القيمة من مصدرها ويقارن.
   - مفاتيح «الكود» (النموذج، الموعد، عدد البحث، التبويبات، قواعد التقييم...): اختلافها مخالفة. تُحدَّث فقط
     بـ --fix داخل PR يغيّر الكود نفسه، فيرى صاحب المنصة التغيير قبل الدمج.
   - مفاتيح «البيانات» (نسب التغطية، سجل الانحياز، الأعطال الجارية...): يحدّثها التشغيل الحي كل 30 دقيقة.
2) قواعد «ما لا يُفعل أبدًا» (المادة 26) تُفحص في كل الملفات المنشورة.
3) قواعد التقييم مقفلة ببصمة: تغييرها دون --relock مع سبب يُسجَّل في سجل القرارات مخالفة (المادة 24.5).
4) سجل الملفات (المادة 5) يُولَّد من هذا الملف: كل ملف في المستودع موصوف، وكل ملف موصوف موجود.
5) مصدر يفشل أكثر من 24 ساعة مخالفة، ويظهر في الأعطال الجارية (المادة 27 مكرر) تلقائيًا.

الاستعمال:
  python constitution_guard.py              فحص فقط (CI): يخرج بـ 1 إن وُجدت مخالفة
  python constitution_guard.py --fix-data   يحدّث مفاتيح البيانات ثم يفحص (التشغيل الحي)
  python constitution_guard.py --fix        يحدّث كل المفاتيح ثم يفحص (داخل PR يغيّر الكود)
  python constitution_guard.py --status     يكتب النتيجة في status.json (تظهر في الصفحة)
  python constitution_guard.py --relock "السبب"   يحدّث بصمة قواعد التقييم ويضيف سطرًا إلى سجل القرارات
  python constitution_guard.py --ci         فحوص إضافية في GitHub Actions (المستودع عام، لا دفع مباشر إلى main)
لا يستعمل إلا مكتبة Python القياسية، ولا يشغّل fetch_gold.py: يقرأ ثوابته بـ ast.
"""
import ast
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CONST, README = "CONSTITUTION.md", "README.md"
DOCS = (CONST, README, "index.html")
MARK = re.compile(r"<!--a:([\w.]+)-->(.*?)<!--/a-->", re.S)
LOCK = re.compile(r"<!--lock:scoring=([0-9a-f]{12})-->")
MONTHS = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")
BIAS_AR = {"neutral": "محايد", "bearish": "هابط", "bullish": "صاعد"}
FAIL_HOURS = 24

# ---------------------------------------------------------------- سجل الملفات (المادة 5)
# كل ملف في المستودع يجب أن يكون هنا، وكل ما هنا يجب أن يكون موجودًا إلا ما عُلّم «مشروط».
FILES = [
    ("data.json", "كل البيانات المعروضة في الصفحة. عام."),
    ("prices.json", "السعر: يومي منذ 2008 وساعي وOHLC. عام."),
    ("status.json", "حالة آخر فحص لكل مصدر، ونتيجة حارس الدستور. عام."),
    ("news.json", "عناوين البث الحي والبيانات الصادرة لهذه المنصة. **مشروط:** يُكتب فقط حين يعمل المستمع هنا (المادة 3)."),
    ("fetch_gold.py", "كل الجلب والحساب والتقرير الذكي."),
    ("index.html", "كل العرض."),
    ("run_loop.sh", "التشغيل الحي: تحديث كل 30 دقيقة، وتسليم الدور، وتسلسل التشغيلات، والأخبار كل 10 دقائق حين يعمل المستمع."),
    ("fj_listen.py", "مستمع FinancialJuice (معطّل هنا بقرار، المادة 3)."),
    ("constitution_guard.py", "حارس الدستور (المادة 24 مكرر)."),
    ("CONSTITUTION.md", "هذه الوثيقة."),
    ("README.md", "تعريف قصير بالمستودع."),
    ("CLAUDE.md", "قواعد الرد على صاحب المنصة لكل جلسة عمل."),
    (".claude/skills/blunt/SKILL.md", "مهارة «الرد الصريح» (المادة 25)."),
    (".gitignore", "ما لا يُرفع إلى المستودع (ملفات Python المؤقتة)."),
    (".github/workflows/update.yml", "التشغيل الحي في GitHub Actions."),
    (".github/workflows/constitution.yml", "فحص الدستور على كل PR وكل دفع إلى main."),
    ("prompts/fable.md", "تعليمات صاحب المنصة للنموذج. يُعدَّل من GitHub مباشرة."),
    ("tradingview/gold_levels.pine", "مؤشر TradingView (المادة 23)."),
    ("data/ai_history.json", "تقارير النموذج المحفوظة."),
    ("data/ai_research_log.json", "سجل بحث النموذج: ما بحث عنه ولماذا وماذا وجد."),
    ("data/calendar_log.json", "حركة الذهب في يوم كل حدث اقتصادي مهم."),
    ("data/comex_history.json", "مخزون COMEX: Registered وEligible وTotal."),
    ("data/deliveries_history.json", "تسليمات COMEX حسب البنك."),
    ("data/etf_history.json", "حيازات IAU اليومية."),
    ("data/f13_history.json", "حيازات المؤسسات في GLD وIAU من 13F."),
    ("data/f13_types.json", "تصنيف المؤسسات في 13F حسب النوع."),
    ("data/filter_log.json", "سجل تقييم فلتر الاتجاه اليومي."),
    ("data/gex_history.json", "سجل GEX اليومي على GLD."),
    ("data/gld_lbma_pm.json", "سعر LBMA PM المشتق من أرشيف GLD (LBMA يحجب الجلب المباشر)."),
    ("data/weekly_brief.json", "خلاصة نشرة خبراء أسبوعية بصياغتنا (لا اقتباس ولا أرقام من النشرة)، تكتبها Routine كل اثنين ويقرؤها التقرير (المادة 15). **مشروط:** تظهر بعد أول تشغيل للـ Routine."),
    ("data/gc_5m.csv", "شموع GC كل 5 دقائق بالحجم، تتراكم من Yahoo (آخر 60 يومًا ثم تُضاف كل يوم)، ومنها Volume Profile وTPO (المادة 3). **مشروط:** يُكتب في أول تشغيل."),
    ("data/gld_close.json", "إغلاق سهم GLD اليومي، لنسبة GC ÷ GLD التي تحوّل مستويات GEX إلى GC (المادة 10). **مشروط:** يُكتب في أول جلب لـ GLD."),
    ("data/gld_ozps.json", "أونصات الذهب لكل سهم GLD شهريًا."),
    ("data/oi_daily.json", "Open Interest والحجم اليومي لعقد GC من CME."),
    ("data/outlook_log.json", "سجل الانحيازات والاحتمالات للتقييم بعد 14 يومًا."),
    ("data/releases.json", "لحظة رصد كل تقرير جديد لكل مصدر."),
    ("data/scorecard.json", "بطاقة التقييم: حكم كل مكوّن حسب قواعد المادة 19."),
    ("data/sge_history.json", "أسعار بورصة شنغهاي للذهب."),
    ("data/source_dates.json", "ذاكرة تواريخ نشر صفحات الأخبار (قانون التاريخ)."),
    ("data/state.json", "حالة التشغيل: آخر فحص لكل مصدر، والتقرير المعلّق، والأعطال."),
    ("data/wgc_etf.json", "تدفقات صناديق الذهب العالمية من WGC."),
    ("data/experiments/*.json", "نتائج التجارب (مثل تجربة Haiku في 3 نوفمبر)."),
]
IGNORE = ("data/raw/*",)


# ---------------------------------------------------------------- قراءة المصادر
def rd(path, default=None):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def rj(path, default=None):
    t = rd(path)
    if t is None:
        return default
    try:
        return json.loads(t)
    except ValueError:
        return default


def code_consts():
    """الثوابت في المستوى الأعلى من fetch_gold.py دون تشغيله: القيم الحرفية، والافتراضي في os.environ.get،
    ومفاتيح القواميس التي قيمها دوال (مثل SECTIONS)."""
    tree = ast.parse(rd("fetch_gold.py", ""))
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            names = [t.id] if isinstance(t, ast.Name) else (
                [e.id for e in t.elts if isinstance(e, ast.Name)] if isinstance(t, ast.Tuple) else [])
            v = node.value
            try:
                val = ast.literal_eval(v)
            except (ValueError, SyntaxError, TypeError):
                if isinstance(v, ast.Call) and len(v.args) == 2 and isinstance(v.args[1], ast.Constant):
                    val = v.args[1].value                    # os.environ.get("X", "default")
                elif isinstance(v, ast.Dict):
                    try:
                        val = {"__keys__": [ast.literal_eval(k) for k in v.keys]}
                    except (ValueError, SyntaxError):
                        continue
                else:
                    continue
            if len(names) == 1:
                out[names[0]] = val
            elif isinstance(val, tuple) and len(val) == len(names):
                out.update(zip(names, val))
    return out


def page_tabs():
    t = rd("index.html", "")
    m = re.search(r"const TABS = \(\) => \[(.*?)\];", t, re.S)
    return re.findall(r"\['(\w+)', '([^']+)'\]", m.group(1)) if m else []


def loop_minutes():
    m = re.search(r"LOOP_MINUTES: .*?'(\d+)'", rd(".github/workflows/update.yml", ""))
    return m and m.group(1)


def model_name(mid):
    m = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", str(mid))
    return f"Claude {m.group(1).capitalize()} {m.group(2)}.{m.group(3)}" if m else str(mid)


def fmt_g(x):
    return f"{x:g}"


def day_ar(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {MONTHS[d.month - 1]}"


def pct(v):
    return "—" if v is None else f"{round(v):d}%"


def scoring_fingerprint(C):
    """بصمة قواعد التقييم وأفقه: أي تغيير فيها يغيّر البصمة."""
    keys = ("SC_RULES", "OUTLOOK_DAYS", "OUTLOOK_BAND", "FILTER_BANDS", "FILTER_SWING", "FILTER_ERAS")
    blob = json.dumps({k: C.get(k) for k in keys}, sort_keys=True, ensure_ascii=False, default=list)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- القيم المحسوبة
def bias_record():
    L = rj("data/outlook_log.json", []) or []
    if not L:
        return "لا تقارير مسجّلة بعد."
    cnt = lambda rows: "، ".join(f"{sum(r.get('bias') == b for r in rows)} «{BIAS_AR[b]}»"
                                 for b in ("neutral", "bearish", "bullish") if any(r.get("bias") == b for r in rows))
    by = {}
    for r in L:
        by.setdefault(model_name(r.get("model")), []).append(r)
    parts = []
    for m, rows in by.items():
        ps = [r["p"] for r in rows if isinstance(r.get("p"), list) and len(r["p"]) == 3]
        rng = lambda j: f"{min(p[j] for p in ps)} إلى {max(p[j] for p in ps)}" if ps else "—"
        parts.append(f"{m}: {len(rows)} ({cnt(rows)}؛ عرضي {rng(0)}، صعود {rng(1)}، هبوط {rng(2)})")
    return (f"{len(L)} تقريرًا مسجّلًا ({day_ar(L[0]['date'])} إلى {day_ar(L[-1]['date'])}): {cnt(L)}. "
            f"حسب النموذج: " + "؛ ".join(parts) + ".")


def faults():
    S = rj("status.json", {}) or {}
    now = dt.datetime.now(dt.timezone.utc)
    rows, hard = [], []
    for name, v in sorted((S.get("sources") or {}).items()):
        if v.get("ok", True) or v.get("skipped"):
            continue
        last = v.get("last_ok")
        hrs = (now - dt.datetime.fromisoformat(last)).total_seconds() / 3600 if last else None
        since = f"آخر نجاح {day_ar(last)} {last[11:16]} UTC" if last else "لم ينجح قط"
        err = re.sub(r"https?://\S+", "", str(v.get("error") or ""))[:90].strip()
        rows.append(f"- `{name}`: يفشل، {since}. الخطأ: {err}")
        if hrs is None or hrs > FAIL_HOURS:
            hard.append(name)
    return ("\n".join(rows) if rows else "لا أعطال جارية في آخر فحص."), hard


def ladder():
    st = ((rj("data.json", {}) or {}).get("direction_filter") or {}).get("stages") or {}
    asof = ((rj("data.json", {}) or {}).get("direction_filter") or {}).get("asof")
    h = st.get("hist") or {}
    if not h or not asof:
        return "لا قراءة بعد."
    names = ("تحت الثلاثة", "فوق واحد", "فوق اثنين", "فوق الثلاثة")
    rows = "، و".join(f"{pct(h[k]['up10'])} {names[int(k)]} ({h[k]['n']:,} حالة)" for k in sorted(h))
    return f"آخر قراءة (إغلاق {day_ar(asof)}، الآن «{names[st.get('now', 0)]}»): أعلى بعد 10 جلسات {rows}."


def computed():
    """كل مفتاح: (النوع، القيمة). code = يتبع الكود ولا يتغير إلا بـ PR؛ data = يتبع البيانات ويحدّثه التشغيل الحي."""
    C = code_consts()
    D = rj("data.json", {}) or {}
    df = D.get("direction_filter") or {}
    SC = C.get("SC_RULES") or {}
    tabs = page_tabs()
    out = {
        "sources": ("code", str(len((C.get("SECTIONS") or {}).get("__keys__", [])))),
        "model": ("code", model_name(C.get("AI_MODEL"))),
        "effort": ("code", str(C.get("AI_EFFORT"))),
        "report_time": ("code", "{}:{:02d}".format(*C.get("DAILY_REPORT_NY", (0, 0)))),
        "searches": ("code", str(C.get("AI_SEARCHES"))),
        "discovery_searches": ("code", str(C.get("DISCOVERY_SEARCHES"))),
        "keep_reports": ("code", str(C.get("AI_KEEP"))),
        "retry_hours": ("code", str(C.get("AI_RETRY_HOURS"))),
        "bands": ("code", " و".join(f"±{fmt_g(b)}" for b in C.get("FILTER_BANDS", ())) + "σ"),
        "zone_width": ("code", f"{fmt_g(C.get('ZONE_WIDTH_SIGMA', 0))}σ"),
        "w_gex": ("code", fmt_g((C.get("LV_WEIGHT") or {}).get("zero_gamma", 0))),
        "w_prior": ("code", fmt_g((C.get("LV_WEIGHT") or {}).get("pdh", 0))),
        "outlook_days": ("code", str(C.get("OUTLOOK_DAYS"))),
        "outlook_band": ("code", f"{fmt_g((C.get('OUTLOOK_BAND') or 0) * 100)}%"),
        "sc_bands_n": ("code", str(SC.get("bands", {}).get("min_n"))),
        "sc_bands_tol": ("code", str(SC.get("bands", {}).get("tol_pts"))),
        "sc_edge_pct": ("code", str(SC.get("edge", {}).get("pass_pct"))),
        "sc_edge_n": ("code", str(SC.get("edge", {}).get("min_n"))),
        "sc_levels_margin": ("code", str(SC.get("levels", {}).get("margin_pts"))),
        "sc_levels_n": ("code", str(SC.get("levels", {}).get("min_touch"))),
        "sc_gex_margin": ("code", fmt_g(SC.get("gex_regime", {}).get("margin_sigma", 0))),
        "sc_gex_n": ("code", str(SC.get("gex_regime", {}).get("min_n"))),
        "sc_prelim": ("code", str(SC.get("fable", {}).get("prelim_windows"))),
        "sc_final": ("code", str(SC.get("fable", {}).get("min_windows"))),
        "tabs_count": ("code", str(len(tabs))),
        "tabs": ("code", "، ".join(n for _, n in tabs)),
        "loop_minutes": ("code", str(loop_minutes())),
        "files": ("code", "\n".join(f"- `{p}`: {d}" for p, d in FILES)),
    }
    for b in df.get("bands") or []:
        out[f"cover_{fmt_g(b['k'])}"] = ("data", pct(b.get("cover")))
    for s in df.get("swing") or []:
        out[f"swing_cover_{s['days']}"] = ("data", pct(s.get("cover")))
    for j, e in enumerate((df.get("stats") or {}).get("edge_eras") or []):
        out[f"edge{j}_up1"] = ("data", pct(e.get("up1")))
        out[f"edge{j}_up10"] = ("data", pct(e.get("up10")))
    out["bias_record"] = ("data", bias_record())
    out["ladder"] = ("data", ladder())
    out["faults"] = ("data", faults()[0])
    return out, C


# ---------------------------------------------------------------- الفحوص
def tracked():
    try:
        r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
        return [x for x in r.stdout.splitlines() if x]
    except (OSError, subprocess.CalledProcessError):
        return []


COST_KEYS = {"usage", "cost", "cost_30d", "cost_usd", "input_tokens", "output_tokens",
             "cache_read_input_tokens", "cache_creation_input_tokens"}
HIDDEN_KEYS = {"hidden_correlations", "hidden_nowcasts", "_wire", "live_wire"}
COST_TEXT = re.compile(r"(?:كلّف|كلفة|تكلفة|تكلّف|\bcost\b)[^\n|]{0,40}?(?:\$\s?\d|\d[\d.,]*\s?(?:دولار|USD))", re.I)


def walk_keys(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, f"{path}/{k}"
            yield from walk_keys(v, f"{path}/{k}")
    elif isinstance(o, list):
        for v in o:
            yield from walk_keys(v, path)


def check(values, C, ci=False):
    V = []      # (معرّف، المادة، الرسالة)
    W = []      # تحذيرات لا تُسقط الفحص
    files = tracked()
    # المادة 26: لا كلفة ولا طبقة مخفية ولا مورّد مدفوع في أي ملف منشور
    for f in files:
        if f.endswith(".json"):
            o = rj(f)
            if o is None:
                continue
            seen = set()
            for k, p in walk_keys(o):
                if k in COST_KEYS and ("cost", p) not in seen:
                    seen.add(("cost", p))
                    V.append(("cost", "26", f"مفتاح كلفة منشور: {f} {p}"))
                if k in HIDDEN_KEYS and ("hidden", p) not in seen:
                    seen.add(("hidden", p))
                    V.append(("hidden", "26", f"حزمة مخفية منشورة: {f} {p}"))
            low = rd(f, "").lower()
            for vendor in ("menthorq", "gexbot"):
                if vendor in low:
                    V.append(("vendor", "26", f"اسم مورّد مدفوع في ملف بيانات منشور: {f} ({vendor})"))
        if f.endswith((".md", ".html")):
            t = rd(f, "")
            for m in COST_TEXT.finditer(t):
                if "Managed Money" not in t[max(0, m.start() - 40):m.end() + 40]:
                    V.append(("cost", "26", f"رقم كلفة في نص منشور: {f}: «{m.group(0)[:60]}»"))
    page = rd("index.html", "")
    for k in HIDDEN_KEYS - {"_wire"}:
        if k in page:
            V.append(("hidden", "26", f"الصفحة تشير إلى حزمة مخفية: {k}"))
    if re.search(r"visitor|عدد الزوار|عدد الزيارات", page, re.I):
        V.append(("counter", "26", "عدّاد زوار في الصفحة"))
    # المادة 16 و26: النموذج لا يكتب أسعارًا؛ المادة 17 مكرر: لكل خبر تاريخ ودرجة
    ai = ((rj("data.json", {}) or {}).get("ai") or {}).get("latest") or {}
    for z in (ai.get("focus") or {}).get("zones") or []:
        if not re.fullmatch(r"Z\d+", str(z.get("zone", ""))):
            V.append(("price", "16", f"منطقة تركيز ليست معرّفًا: {z.get('zone')!r}"))
    for e in (ai.get("ar") or {}).get("events") or []:
        if not e.get("date"):
            V.append(("date", "17 مكرر", f"خبر بلا تاريخ: {str(e.get('title'))[:50]}"))
        if not e.get("tier"):
            V.append(("tier", "17 مكرر", f"خبر بلا درجة مصدر: {str(e.get('title'))[:50]}"))
    # المادة 6 و26: التشغيل اليدوي لا يوقف الآلي
    loop = rd("run_loop.sh", "")
    for need, msg in (("manual_waiting", "تسليم الدور للتشغيل اليدوي"), ("gh workflow run update.yml", "تسلسل التشغيلات")):
        if need not in loop:
            V.append(("auto", "6", f"run_loop.sh فقد: {msg}"))
    if "gh workflow enable" not in rd(".github/workflows/update.yml", ""):
        V.append(("auto", "6", "update.yml فقد خطوة إبقاء التحديث التلقائي مفعّلًا"))
    # المادة 24.5: قواعد التقييم مقفلة
    m = LOCK.search(rd(CONST, ""))
    fp = scoring_fingerprint(C)
    if not m:
        V.append(("lock", "24", "بصمة قواعد التقييم غائبة من الدستور"))
    elif m.group(1) != fp:
        V.append(("lock", "24", f"قواعد التقييم تغيّرت ({m.group(1)} ← {fp}) دون --relock وسطر في سجل القرارات"))
    # المفاتيح: كل مفتاح موجود في الدستور، وقيمته مطابقة
    present = {}
    for doc in DOCS:
        for k, v in MARK.findall(rd(doc, "")):
            present.setdefault(k, []).append((doc, v))
    for k, (kind, val) in values.items():
        if k not in present:
            V.append(("marker", "—", f"المفتاح {k} غائب من الدستور"))
            continue
        for doc, v in present[k]:
            if v.strip() != val.strip():
                (V if kind == "code" else W).append(
                    ("drift", "—", f"{doc}: {k} مكتوب «{v.strip()[:50]}» والحقيقة «{val.strip()[:50]}»"))
    for k in present:
        if k not in values:
            V.append(("marker", "—", f"مفتاح لا يعرفه الحارس: {k}"))
    # المادة 5: كل ملف موصوف، وكل موصوف موجود
    pats = [p for p, _ in FILES]
    for f in files:
        if not any(fnmatch.fnmatch(f, p) for p in pats + list(IGNORE)):
            V.append(("files", "5", f"ملف غير موصوف في الدستور: {f}"))
    for p, d in FILES:
        if "*" in p or "مشروط" in d:
            continue
        if files and p not in files and not os.path.exists(os.path.join(ROOT, p)):
            V.append(("files", "5", f"ملف موصوف غير موجود: {p}"))
    # المادة 27 مكرر: مصدر يفشل أكثر من 24 ساعة
    for name in faults()[1]:
        V.append(("source", "27 مكرر", f"المصدر {name} يفشل منذ أكثر من {FAIL_HOURS} ساعة"))
    if ci:
        V += ci_checks()
    return V, W


def ci_checks():
    """فحوص لا تُجرى إلا في GitHub Actions."""
    V = []
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if repo and token:
        try:
            r = subprocess.run(["gh", "api", f"repos/{repo}", "--jq", ".private"], capture_output=True, text=True, timeout=60)
            if r.stdout.strip() == "true":
                V.append(("private", "26", "المستودع صار خاصًا"))
        except (OSError, subprocess.SubprocessError):
            pass
    if os.environ.get("GITHUB_EVENT_NAME") == "push" and os.environ.get("GITHUB_REF") == "refs/heads/main":
        r = subprocess.run(["git", "log", "-1", "--format=%an|%s"], cwd=ROOT, capture_output=True, text=True)
        author, _, subject = r.stdout.strip().partition("|")
        d = subprocess.run(["git", "diff", "--name-only", "HEAD~1", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        changed = [x for x in d.stdout.splitlines() if x]
        owner_prompt = changed and all(x.startswith("prompts/") or x == "data/weekly_brief.json" for x in changed)
        # المادة 5: تعليمات صاحب المنصة يعدّلها مباشرة، وخلاصة النشرة الأسبوعية تكتبها الـ Routine مباشرة
        if author != "gold-data-bot" and not subject.startswith("Merge pull request #") and not owner_prompt:
            V.append(("push", "24", f"دفع مباشر إلى main دون PR: «{subject[:60]}» ({author})"))
    return V


# ---------------------------------------------------------------- الكتابة
def fix(values, kinds):
    changed = []
    for doc in DOCS:
        t = rd(doc)
        if t is None:
            continue

        def sub(m):
            k, old = m.group(1), m.group(2)
            if k in values and values[k][0] in kinds and old.strip() != values[k][1].strip():
                changed.append(f"{doc}:{k}")
                new = values[k][1]
                return f"<!--a:{k}-->{chr(10) + new + chr(10) if chr(10) in old or chr(10) in new else new}<!--/a-->"
            return m.group(0)

        nt = MARK.sub(sub, t)
        if nt != t:
            with open(os.path.join(ROOT, doc), "w", encoding="utf-8") as f:
                f.write(nt)
    return changed


def relock(C, reason):
    t = rd(CONST, "")
    fp = scoring_fingerprint(C)
    if not LOCK.search(t):
        raise SystemExit("لا توجد علامة القفل في الدستور")
    t = LOCK.sub(f"<!--lock:scoring={fp}-->", t, count=1)
    row = f"| {day_ar(dt.date.today().isoformat())} | إعادة قفل قواعد التقييم (البصمة {fp}) | {reason} |\n"
    anchor = "\n\n---\n\n*تُراجع"                    # نهاية جدول سجل القرارات
    if anchor not in t:
        raise SystemExit("لم يُعثر على نهاية سجل القرارات")
    t = t.replace(anchor, "\n" + row.rstrip("\n") + anchor, 1)
    with open(os.path.join(ROOT, CONST), "w", encoding="utf-8") as f:
        f.write(t)
    print(f"relocked: {fp}")


def write_status(V, W, changed):
    S = rj("status.json", {}) or {}
    S["constitution"] = {"ok": not V, "checked_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                         "violations": [{"id": i, "article": a, "msg": m} for i, a, m in V],
                         "warnings": [m for _, _, m in W], "updated": changed}
    with open(os.path.join(ROOT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False, separators=(",", ":"))


def main(argv):
    values, C = computed()
    if "--relock" in argv:
        i = argv.index("--relock")
        reason = " ".join(argv[i + 1:]).strip()
        if not reason:
            raise SystemExit("--relock يحتاج سببًا مكتوبًا")
        relock(C, reason)
        return 0
    changed = []
    if "--fix" in argv:
        changed = fix(values, {"code", "data"})
    elif "--fix-data" in argv:
        changed = fix(values, {"data"})
    V, W = check(values, C, ci="--ci" in argv)
    if "--status" in argv:
        write_status(V, W, changed)
    for c in changed:
        print("[guard] updated", c)
    for _, _, m in W:
        print("[guard] warning:", m)
    for i, a, m in V:
        print(f"[guard] VIOLATION ({i}, المادة {a}): {m}")
    print(f"[guard] {'ok' if not V else str(len(V)) + ' violation(s)'}; {len(W)} warning(s)")
    return 1 if V else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
