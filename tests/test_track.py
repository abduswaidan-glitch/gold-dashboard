"""اختبارات انحدار لسجل أداء الانحياز (المادة 19 والمادة 20). المكتبة القياسية فقط، وبلا شبكة.

تعمل على نسخة مؤقتة من المستودع ولا تكتب فيه شيئًا. التشغيل:
    python tests/test_track.py                 الاختبارات
    python tests/test_track.py --fixtures DIR  الاختبارات، ثم ملفات data.json لاختبار الواجهة (tests/track_ui.js)

ما تثبته:
1) النوافذ المستقلة في حكم «انحياز النموذج» تُحسب من السجل المقيَّم كاملًا: 262 توقعًا يوميًا → 28 نافذة، لا 4.
2) سجل الأداء يصل النموذج من مرآة الأداء وحدها، بعد 10 توقعات مقيَّمة: 1 و9 → لا شيء، 10 → المرآة لا الملخص.
3) brier_base وbrier_paired والمهارة تطابق حسابًا مستقلًا، على توقعات واحتمالات ومعدلات تاريخية متنوعة.
4) قواعد التقييم وأفقها ونطاقها وعتباتها كما هي، وبصمة القفل مطابقة للدستور.
"""
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def setup():
    tmp = tempfile.mkdtemp(prefix="gold-track-")
    for name in ("fetch_gold.py", "constitution_guard.py", "CONSTITUTION.md", "prices.json", "data.json"):
        shutil.copy(os.path.join(ROOT, name), tmp)
    for d in ("data", "prompts"):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d))
    os.chdir(tmp)
    sys.path.insert(0, tmp)
    import fetch_gold as F
    import constitution_guard as G
    F.log = lambda *a, **k: None
    # الحزمتان المخفيتان تجلبان من الشبكة ولا علاقة لهما بسجل الأداء: تُعطَّلان في الاختبار فقط
    F.correlation_pack = lambda d: None
    F.nowcast_pack = lambda d: None
    return tmp, F, G


def synthetic(F, n=None, varied=False):
    """توقع لكل جلسة من 2025-10-01 إلى 2026-10-15، كلها قابلة للتقييم على إغلاقات GC الحقيقية في prices.json."""
    daily = [(d, v) for d, v in json.load(open("prices.json"))["daily"] if v]
    px = dict(daily)
    days = [d for d, _ in daily if "2025-10-01" <= d <= "2026-10-15"][:n]
    ps = [[40, 30, 30], [20, 55, 25], [50, 30, 20], [34, 33, 33], [15, 25, 60]]
    brs = [{"up": 33, "flat": 47, "down": 21}, {"up": 28, "flat": 47, "down": 25}, {"up": 32, "flat": 51, "down": 17}]
    out = []
    for i, d in enumerate(days):
        p = ps[i % len(ps)] if varied else [40, 30, 30]
        out.append({"id": f"{d}T14:00:00+00:00", "date": d, "model": "claude-opus-5-5", "bias": "neutral", "p": p,
                    "dirs": ["flat", "up", "down"], "base": {"date": d, "price": px[d]},
                    "base_rates": brs[i % len(brs)] if varied else {"up": 33, "flat": 34, "down": 33}})
    return out


def track_for(F, log_):
    json.dump(log_, open(F.OUTLOOK_LOG, "w"))
    return F.score_outlooks()


def data_with(track):
    d = json.load(open("data.json"))
    d["outlook_track"] = track
    return d


def test_windows(F):
    log_ = synthetic(F)
    tr = track_for(F, log_)
    sc = F.scorecard(data_with(tr))
    scored = [e for e in json.load(open(F.OUTLOOK_LOG)) if e.get("result")]
    full = F._windows([e["base"]["date"] for e in scored])
    rows_only = F._windows([e["base"]["date"] for e in tr["rows"] if e.get("result")])
    check(len(log_) == 262 and len(scored) == 262, f"262 synthetic forecasts, all scored (got {len(log_)}, {len(scored)})")
    check(full == 28, f"independent windows on the full scored log = 28 (got {full})")
    check(rows_only == 4, f"the old computation on the last 30 rows gives 4 (got {rows_only}): the bug this fixes")
    check(sc["fable"]["independent_windows"] == full == tr["summary"]["windows"],
          f"scorecard() uses the full log: {sc['fable']['independent_windows']} == {full} == summary.windows")
    check(sc["fable_direction"]["independent_windows"] == tr["summary"]["direction"]["windows"],
          "the direction verdict still reads its own full-log windows")
    check(sc["fable"]["verdict"] in ("pass", "fail"), f"with 28 windows the locked rule reaches a final verdict ({sc['fable']['verdict']})")


def test_real_log(F):
    shutil.copy(os.path.join(ROOT, "data", "outlook_log.json"), F.OUTLOOK_LOG)
    tr = F.score_outlooks()
    sc = F.scorecard(data_with(tr))
    scored = [e for e in json.load(open(F.OUTLOOK_LOG)) if e.get("result")]
    full = F._windows([e["base"]["date"] for e in scored if e.get("base")])
    check(sc["fable"]["independent_windows"] == full, f"real log ({len(scored)} scored): scorecard windows == full-log windows ({full})")


def test_mirror(F):
    for k in (1, 9, 10):
        tr = track_for(F, synthetic(F, k))
        g = F.ai_digest(data_with(tr))
        sent, mirror = g.get("your_track_record"), F.performance_mirror()
        if k < F.MIRROR_MIN:
            check(sent is None and mirror is None, f"{k} scored: nothing reaches the model (got {type(sent).__name__})")
        else:
            check(sent is not None and sent == mirror, f"{k} scored: the model gets performance_mirror()")
            check(sent != tr["summary"] and "independent_14d_windows" in sent and "n" not in sent,
                  f"{k} scored: not the raw summary (keys: {sorted(sent)[:4]}...)")


def brier(p, out):
    return sum((p.get(k, 0) - (k == out)) ** 2 for k in ("up", "flat", "down"))


def test_brier_base(F):
    tr = track_for(F, synthetic(F, varied=True))
    s = tr["summary"]
    rows = [e for e in json.load(open(F.OUTLOOK_LOG)) if e.get("result") and e.get("base_rates")]
    bm, bb = [], []
    for e in rows:
        out = e["result"]["outcome"]
        bm.append(brier(dict(zip(e["dirs"], [x / 100 for x in e["p"]])), out))
        tot = sum(e["base_rates"].values())
        bb.append(brier({k: v / tot for k, v in e["base_rates"].items()}, out))
    m, b = sum(bm) / len(bm), sum(bb) / len(bb)
    check(s["n_base"] == len(rows), f"n_base = {len(rows)} forecasts that carry base rates")
    check(abs(s["brier_paired"] - m) < 5e-4, f"brier_paired {s['brier_paired']} == independent {m:.4f}")
    check(abs(s["brier_base"] - b) < 5e-4, f"brier_base {s['brier_base']} == independent {b:.4f} (base rates normalised; they sum to 101)")
    check(abs(s["skill"] - (1 - m / b) * 100) < 0.06, f"skill {s['skill']}% == 1 - paired/base = {(1 - m / b) * 100:.2f}%")
    # حالة بحساب يدوي: 40/30/30 للعرضي/الصعود/الهبوط، والمعدل 33/47/21 (مجموعه 101)، والنتيجة «عرضي»
    hand_m = 0.3 ** 2 + (1 - 0.4) ** 2 + 0.3 ** 2
    hand_b = (33 / 101) ** 2 + (1 - 47 / 101) ** 2 + (21 / 101) ** 2
    check(abs(brier({"flat": .4, "up": .3, "down": .3}, "flat") - hand_m) < 1e-12 and abs(hand_b - 0.4358) < 1e-4,
          f"hand case: model {hand_m:.4f}, base rate {hand_b:.4f}")


def test_rules_and_lock(F, G):
    c = G.code_consts()
    check(F.OUTLOOK_DAYS == 14 and F.OUTLOOK_BAND == 0.02, "OUTLOOK_DAYS = 14, OUTLOOK_BAND = 0.02")
    check(F.SC_RULES["fable"] == {"prelim_windows": 13, "min_windows": 26}
          and F.SC_RULES["fable_direction"] == {"prelim_windows": 13, "min_windows": 26}, "SC_RULES thresholds 13 and 26")
    lock = re.search(r"<!--lock:scoring=([0-9a-f]{12})-->", open("CONSTITUTION.md", encoding="utf-8").read()).group(1)
    fp = G.scoring_fingerprint(c)
    check(fp == lock, f"scoring lock unchanged: {fp} == {lock}")


def fixtures(F, out):
    os.makedirs(out, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "data", "outlook_log.json"), F.OUTLOOK_LOG)
    cases = {"real": F.score_outlooks()}
    for name, n in (("n10", 10), ("prelim", 200), ("final", None)):
        cases[name] = track_for(F, synthetic(F, n, varied=True))
    old = json.loads(json.dumps(cases["final"]))      # بيانات قبل هذا الإصلاح: الملخص بلا windows
    for k in ("windows", "windows_prelim", "windows_final", "brier_paired"):
        old["summary"].pop(k, None)
    cases["old"] = old
    for name, tr in cases.items():
        json.dump(data_with(tr), open(os.path.join(out, f"data_{name}.json"), "w"), ensure_ascii=False)
        print(f"fixture data_{name}.json: scored {tr['summary']['n']}, windows {tr['summary'].get('windows')}")


if __name__ == "__main__":
    out = sys.argv[sys.argv.index("--fixtures") + 1] if "--fixtures" in sys.argv else None
    if out:
        out = os.path.abspath(out)
    tmp, F, G = setup()
    try:
        test_windows(F)
        test_real_log(F)
        test_mirror(F)
        test_brier_base(F)
        test_rules_and_lock(F, G)
        if out:
            fixtures(F, out)
    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
