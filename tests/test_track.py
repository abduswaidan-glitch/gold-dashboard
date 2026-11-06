"""اختبارات انحدار لسجل أداء الانحياز (المادتان 19 و20). بلا شبكة. الاختبار بالمكتبة القياسية، ويحتاج requests
لأن fetch_gold.py يستوردها في أعلاه.

تعمل على نسخة مؤقتة من المستودع ولا تكتب فيه شيئًا. التشغيل:
    python tests/test_track.py                 الاختبارات
    python tests/test_track.py --fixtures DIR  الاختبارات، ثم ملفات data.json لاختبار الواجهة (tests/track_ui.js)

ما تثبته، على مخرجات score_outlooks() (ملخص data.json) وscorecard() (data/scorecard.json) نفسها، وهما ما يقرؤه البريد:
1) النوافذ المستقلة من السجل المقيَّم كاملًا: 262 توقعًا يوميًا → 28 نافذة، لا 4.
2) حكم «انحياز النموذج» يُعدّ على النوافذ التي لها معدل تاريخي: بلا معدل يبقى «لم يكتمل» مهما طال السجل، وبعدد قليل
   منها كذلك؛ ومعها: «أولي» عند 13 و«نهائي» عند 26.
3) قبل 13 نافذة لا متوسطات ولا beats_guessing (null)، في الإجمالي ولكل نموذج ولكل نسخة تعليمات.
4) سجل الأداء يصل النموذج من مرآة الأداء وحدها بعد 10 توقعات مقيَّمة: 1 و9 → لا شيء، 10 → المرآة.
5) brier_base وbrier_paired والمهارة تطابق حسابًا مستقلًا وحالة يدوية.
5أ) كل سجل فردي في fable.scored_this_week يحمل نموذج ذلك التوقع من السجل نفسه، لا model_now.
5ب) fable_direction: قبل 13 نافذة حاسمة not_yet وmetrics_withheld وكل المتوسطات فارغة؛ عند 13 و26 تظهر وتطابق حسابًا
   مستقلًا، والحكم يتبع القاعدة المكتوبة كما هي (المهارة إن حُسبت، وإلا إصابة > 50% وBrier < 0.25).
6) قواعد التقييم وأفقها ونطاقها وعتباتها كما هي، وبصمة القفل مطابقة للدستور.
"""
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
AGG = ("hit", "brier", "brier_naive", "p_outcome", "brier_base", "brier_paired", "skill")


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


def session_days(start="2025-10-01"):
    """جلسات GC من start إلى 2026-10-15: كلها قابلة للتقييم على الإغلاقات الحقيقية في prices.json."""
    return [d for d, v in json.load(open("prices.json"))["daily"] if v and start <= d <= "2026-10-15"]


def synthetic(n=None, varied=False, base=True, base_from=None, start="2025-10-01"):
    """توقع لكل جلسة. base=False: بلا base_rates (كتوقعات ما قبل 1 نوفمبر). base_from: المعدل التاريخي من هذا التاريخ فقط."""
    px = dict((d, v) for d, v in json.load(open("prices.json"))["daily"] if v)
    ps = [[40, 30, 30], [20, 55, 25], [50, 30, 20], [34, 33, 33], [15, 25, 60]]
    brs = [{"up": 33, "flat": 47, "down": 21}, {"up": 28, "flat": 47, "down": 25}, {"up": 32, "flat": 51, "down": 17}]
    out = []
    for i, d in enumerate(session_days(start)[:n]):
        e = {"id": f"{d}T14:00:00+00:00", "date": d, "model": "claude-opus-5-5", "bias": "neutral",
             "p": ps[i % len(ps)] if varied else [40, 30, 30], "dirs": ["flat", "up", "down"],
             "base": {"date": d, "price": px[d]}}
        if base and (base_from is None or d >= base_from):
            e["base_rates"] = brs[i % len(brs)] if varied else {"up": 33, "flat": 34, "down": 33}
        out.append(e)
    return out


def first_n_with_windows(F, k):
    """أقل عدد من التوقعات اليومية يعطي k نافذة مستقلة بالضبط."""
    ds = session_days()
    for n in range(1, len(ds) + 1):
        if F._windows(ds[:n]) == k:
            return n
    raise AssertionError(f"no prefix gives {k} windows")


def run(F, log_):
    json.dump(log_, open(F.OUTLOOK_LOG, "w"))
    tr = F.score_outlooks()
    d = json.load(open("data.json"))
    d["outlook_track"] = tr
    full = F.scorecard(d)
    run.last = full                       # scorecard كاملًا (fable_direction) لآخر تشغيل
    return tr, full["fable"], d


def no_aggregates(summary, sc, tag):
    """لا متوسط ولا beats_guessing في الملخص ولا في scorecard ولا في أي مجموعة."""
    check(not any(k in summary for k in AGG) and summary.get("metrics_withheld") is True,
          f"{tag}: summary carries no averages ({sorted(k for k in AGG if k in summary)})")
    check(all(sc.get(k) is None for k in ("hit_pct", "brier", "brier_guess", "beats_guessing", "brier_paired", "brier_base_rate",
                                       "skill_vs_base_pct")) and sc.get("metrics_withheld") is True,
          f"{tag}: scorecard hit/brier/brier_guess/beats_guessing/base comparison are null")
    groups = list(summary.get("by_model", {}).values()) + list(summary.get("by_prompt", {}).values())
    check(groups and not any(k in g for g in groups for k in AGG), f"{tag}: no averages per model or per prompt version")
    check('"beats_guessing": true' not in json.dumps(sc), f"{tag}: the email file never says beats_guessing=true")
    check(set(summary.get("direction", {})) <= {"n", "n_lean", "windows"}, f"{tag}: direction carries counts only")


def test_windows_262(F):
    log_ = synthetic()
    tr, sc, _ = run(F, log_)
    scored = [e for e in json.load(open(F.OUTLOOK_LOG)) if e.get("result")]
    full = F._windows([e["base"]["date"] for e in scored])
    rows_only = F._windows([e["base"]["date"] for e in tr["rows"] if e.get("result")])
    check(len(log_) == 262 and len(scored) == 262, f"262 synthetic forecasts with base rates, all scored ({len(log_)}, {len(scored)})")
    check(full == 28 and rows_only == 4, f"full scored log = 28 windows; the old last-30-rows count = 4 (got {full}, {rows_only})")
    check(sc.get("independent_windows") == full == tr["summary"].get("windows") and sc.get("windows_with_base_rate") == 28,
          f"scorecard(): {sc.get('independent_windows')} windows, {sc.get('windows_with_base_rate')} with a base rate")
    check(sc.get("verdict") in ("pass", "fail") and sc.get("skill_vs_base_pct") is not None,
          f"with 28 base-rate windows and a computed skill ({sc.get('skill_vs_base_pct')}%) the locked rule gives a final verdict ({sc.get('verdict')})")


def test_no_base_262(F):
    tr, sc, _ = run(F, synthetic(base=False))
    s = tr["summary"]
    check(sc.get("independent_windows") == 28 and sc.get("windows_with_base_rate") == 0, "262 forecasts without base rates: 28 windows, 0 with a base rate")
    check(sc.get("verdict") == "not_yet" and sc.get("skill_vs_base_pct") is None, f"no base rate -> verdict not_yet, not fail (got {sc.get('verdict')})")
    check(sc.get("brier_paired") is None and sc.get("brier_base_rate") is None and "brier_paired" not in s and "skill" not in s,
          "no paired comparison and no skill anywhere")
    check(sc.get("hit_pct") is not None and sc.get("metrics_withheld") is False, "28 windows: the descriptive averages are available")


def test_thresholds(F):
    for k in (13, 26):
        n = first_n_with_windows(F, k)
        tr, sc, _ = run(F, synthetic(n))
        want = ("preliminary_positive", "preliminary_negative") if k == 13 else ("pass", "fail")
        check(sc.get("independent_windows") == k == sc.get("windows_with_base_rate"), f"{k} windows with base rates ({n} forecasts)")
        check(sc.get("verdict") in want and sc.get("brier_paired") is not None and sc.get("hit_pct") is not None,
              f"{k} windows with base rates -> {sc.get('verdict')}, comparison and averages shown")
        tr, sc, _ = run(F, synthetic(n, base=False))
        check(sc.get("verdict") == "not_yet" and sc.get("skill_vs_base_pct") is None and sc.get("brier_paired") is None,
              f"{k} windows without base rates -> not_yet, no comparison (got {sc.get('verdict')})")
        check(sc.get("hit_pct") is not None and sc.get("beats_guessing") is not None, f"{k} windows without base rates: descriptive averages only")


def test_few_base(F):
    n26 = first_n_with_windows(F, 26)
    ds = session_days()[:n26]
    tail = next(d for i, d in enumerate(ds) if F._windows(ds[i:]) == 3)      # المعدل التاريخي في آخر 3 نوافذ فقط
    tr, sc, _ = run(F, synthetic(n26, base_from=tail))
    s = tr["summary"]
    check(sc.get("independent_windows") == 26 and sc.get("windows_with_base_rate") == 3, f"26 windows, only 3 with a base rate (from {tail})")
    check(sc.get("verdict") == "not_yet" and sc.get("skill_vs_base_pct") is None, f"few base-rate windows -> not_yet (got {sc.get('verdict')})")
    check("brier_paired" not in s and "brier_base" not in s and s.get("n_base", 0) > 0, f"summary withholds the comparison ({s.get('n_base')} based forecasts)")


def test_small_samples(F):
    for k in (1, 9, 10):
        tr, sc, data = run(F, synthetic(k, varied=True))
        check(sc.get("verdict") == "not_yet" and sc.get("scored") == k, f"{k} scored: verdict not_yet")
        no_aggregates(tr["summary"], sc, f"{k} scored")
        direction_withheld(run.last["fable_direction"], tr["summary"], f"{k} scored")
        g = F.ai_digest(data)
        sent, mirror = g.get("your_track_record"), F.performance_mirror()
        if k < F.MIRROR_MIN:
            check(sent is None and mirror is None, f"{k} scored: nothing reaches the model (got {type(sent).__name__})")
        else:
            check(sent is not None and sent == mirror and "independent_14d_windows" in sent and "n" not in sent,
                  f"{k} scored: the model gets performance_mirror(), not the summary")


DIR_FIELDS = ("lean_hit_pct", "brier", "brier_coin", "brier_base_rate", "skill_vs_base_pct")
DIR_START = "2024-01-02"                  # من 2025-10 لا تتجاوز النوافذ الحاسمة 25، فيحتاج اختبار 26 تاريخًا أطول


def direction_withheld(D, summary, tag):
    """قبل 13 نافذة حاسمة: not_yet، وmetrics_withheld، وكل المتوسطات فارغة، والملخص يحمل أعدادًا فقط."""
    check(D.get("verdict") == "not_yet" and D.get("metrics_withheld") is True,
          f"{tag}: fable_direction not_yet with metrics_withheld ({D.get('verdict')}, {D.get('metrics_withheld')})")
    check(all(D.get(k) is None for k in DIR_FIELDS), f"{tag}: fable_direction {', '.join(DIR_FIELDS)} are null")
    check(set(summary.get("direction") or {}) <= {"n", "n_lean", "windows"}, f"{tag}: summary.direction carries counts only")


def lean_stats(entries):
    """حساب مستقل لقراءة الاتجاه: الإصابة بين التوقعات المائلة، وBrier الثنائي لحصة الصعود، وBrier المعدل التاريخي."""
    hits, br, brb = [], [], []
    for e in entries:
        if not e.get("result") or e["result"]["outcome"] == "flat":
            continue
        p = dict(zip(e["dirs"], e["p"]))
        up = e["result"]["outcome"] == "up"
        if p["up"] != p["down"]:
            hits.append((p["up"] > p["down"]) == up)
        br.append(((p["up"] / (p["up"] + p["down"]) if p["up"] + p["down"] else 0.5) - up) ** 2)
        b = e.get("base_rates") or {}
        if b.get("up", 0) + b.get("down", 0):
            brb.append((b["up"] / (b["up"] + b["down"]) - up) ** 2)
    return hits, br, brb


def first_n_with_dir_windows(F, k):
    """أقل عدد من التوقعات (من DIR_START) يعطي k نافذة حاسمة مستقلة بالضبط. النتائج من التقييم الفعلي."""
    json.dump(synthetic(varied=True, start=DIR_START), open(F.OUTLOOK_LOG, "w"))
    F.score_outlooks()
    scored = json.load(open(F.OUTLOOK_LOG))
    for n in range(1, len(scored) + 1):
        if F._windows([e["base"]["date"] for e in scored[:n] if e.get("result") and e["result"]["outcome"] != "flat"]) == k:
            return n
    raise AssertionError(f"no prefix gives {k} decisive windows")


def test_direction(F):
    need, fin = F.SC_RULES["fable_direction"]["prelim_windows"], F.SC_RULES["fable_direction"]["min_windows"]
    for k in (12, need, fin):
        n = first_n_with_dir_windows(F, k)
        for base in (True, False):
            tr, _, _ = run(F, synthetic(n, varied=True, base=base, start=DIR_START))
            D, s = run.last["fable_direction"], tr["summary"]
            tag = f"{k} decisive windows {'with' if base else 'without'} base rates ({n} forecasts)"
            check(D.get("independent_windows") == k, f"{tag}: independent_windows = {D.get('independent_windows')}")
            if k < need:
                direction_withheld(D, s, tag)
                continue
            hits, br, brb = lean_stats(json.load(open(F.OUTLOOK_LOG)))
            hit, brier = round(sum(hits) / len(hits) * 100), round(sum(br) / len(br), 3)
            check(D.get("metrics_withheld") is False and D.get("lean_hit_pct") == hit and D.get("brier") == brier
                  and D.get("brier_coin") == 0.25, f"{tag}: lean hit {D.get('lean_hit_pct')}% and Brier {D.get('brier')} == independent {hit}%, {brier}; coin 0.25")
            if base:
                bb = sum(brb) / len(brb)
                # outlook_direction تحسب المهارة من Brier بعد تقريبه إلى 3 منازل (كما هي، لا يغيّرها هذا الـ PR)؛
                # الفرق عن القيمة الكاملة أقل من ربع نقطة مئوية هنا، ويجب ألا يقلب الإشارة
                sk_code, sk_exact = round((1 - brier / bb) * 100, 1), (1 - (sum(br) / len(br)) / bb) * 100
                check(D.get("brier_base_rate") == round(bb, 3) and D.get("skill_vs_base_pct") == sk_code,
                      f"{tag}: base-rate Brier {D.get('brier_base_rate')} and skill {D.get('skill_vs_base_pct')}% match "
                      f"(from the rounded Brier; exact {sk_exact:.2f}%)")
                check((sk_code > 0) == (sk_exact > 0), f"{tag}: rounding does not flip the skill sign")
            else:
                check(D.get("brier_base_rate") is None and D.get("skill_vs_base_pct") is None, f"{tag}: no base-rate comparison")
            # القاعدة المكتوبة (المادة 19) كما هي: المهارة > 0 إن حُسبت، وإلا إصابة > 50% وBrier < 0.25
            sk = D.get("skill_vs_base_pct")
            good = sk > 0 if sk is not None else (D["lean_hit_pct"] > 50 and D["brier"] < 0.25)
            want = ("preliminary_positive" if good else "preliminary_negative") if k < fin else ("pass" if good else "fail")
            check(D.get("verdict") == want, f"{tag}: verdict {D.get('verdict')} follows the written rule ({want})")


def test_scored_this_week_model(F):
    """كل سجل في fable.scored_this_week يحمل النموذج الذي كتب التوقع نفسه، لا model_now."""
    log_ = synthetic(60, varied=True)
    models = ["claude-fable-5-1", "claude-opus-5-5", None]
    for i, e in enumerate(log_):
        m = models[i % 3]
        if m is None:
            e.pop("model", None)          # توقع قديم بلا حقل model
        else:
            e["model"] = m
    by_date = {e["date"]: e.get("model") for e in log_}
    json.dump(log_, open(F.OUTLOOK_LOG, "w"))
    tr = F.score_outlooks()
    d = json.load(open("data.json"))
    d["outlook_track"] = tr
    last_result = max(e["result"]["date"] for e in tr["rows"] if e.get("result"))
    now = dt.datetime.fromisoformat(last_result + "T12:00:00+00:00")     # أسبوع ينتهي عند آخر تقييم
    week = F.scorecard(d, now=now)["fable"]["scored_this_week"]
    keys = {"date", "bias", "p", "outcome", "ret", "brier", "model"}
    check(len(week) > 0, f"scored_this_week has records for the week ending {last_result} ({len(week)})")
    check(all(set(r) == keys for r in week), f"each record has exactly {sorted(keys)}")
    check(all(r["model"] == by_date[r["date"]] for r in week), "each record's model is the model of that forecast")
    fable = [r for r in week if by_date[r["date"]] == "claude-fable-5-1"]
    none = [r for r in week if by_date[r["date"]] is None]
    check(fable and all(r["model"] == "claude-fable-5-1" != F.AI_MODEL for r in fable),
          f"a Fable forecast stays Fable, not model_now {F.AI_MODEL} ({len(fable)} records)")
    check(none and all(r["model"] is None for r in none), f"a forecast with no model gives null, not model_now ({len(none)} records)")


def test_real_log(F):
    shutil.copy(os.path.join(ROOT, "data", "outlook_log.json"), F.OUTLOOK_LOG)
    tr = F.score_outlooks()
    d = json.load(open("data.json"))
    d["outlook_track"] = tr
    sc = F.scorecard(d)["fable"]
    scored = [e for e in json.load(open(F.OUTLOOK_LOG)) if e.get("result")]
    full = F._windows([e["base"]["date"] for e in scored if e.get("base")])
    check(sc.get("independent_windows") == full, f"real log ({len(scored)} scored): scorecard windows == full-log windows ({full})")
    if full < 13:
        no_aggregates(tr["summary"], sc, "real log")
    log_models = {e["date"]: e.get("model") for e in json.load(open(F.OUTLOOK_LOG))}
    wk = F.scorecard(d)["fable"].get("scored_this_week") or []
    check(all(r.get("model") == log_models.get(r["date"]) for r in wk), f"real log: scored_this_week models match the log ({len(wk)} records)")
    D = F.scorecard(d)["fable_direction"]
    if D.get("independent_windows", 0) < 13:
        direction_withheld(D, tr["summary"], "real log")


def brier(p, out):
    return sum((p.get(k, 0) - (k == out)) ** 2 for k in ("up", "flat", "down"))


def test_brier_base(F):
    tr, _, _ = run(F, synthetic(varied=True))
    s = tr["summary"]
    rows = [e for e in json.load(open(F.OUTLOOK_LOG)) if e.get("result") and e.get("base_rates")]
    bm, bb = [], []
    for e in rows:
        out = e["result"]["outcome"]
        bm.append(brier(dict(zip(e["dirs"], [x / 100 for x in e["p"]])), out))
        tot = sum(e["base_rates"].values())
        bb.append(brier({k: v / tot for k, v in e["base_rates"].items()}, out))
    m, b = sum(bm) / len(bm), sum(bb) / len(bb)
    check(s.get("n_base") == len(rows), f"n_base = {len(rows)} forecasts that carry base rates")
    check(abs(s["brier_paired"] - m) < 5e-4, f"brier_paired {s.get('brier_paired')} == independent {m:.4f}")
    check(abs(s["brier_base"] - b) < 5e-4, f"brier_base {s.get('brier_base')} == independent {b:.4f} (base rates normalised; they sum to 101)")
    check(abs(s["skill"] - (1 - m / b) * 100) < 0.06, f"skill {s.get('skill')}% == 1 - paired/base = {(1 - m / b) * 100:.2f}%")
    # حالة يدوية: 40/30/30 للعرضي/الصعود/الهبوط، والمعدل 33/47/21 (مجموعه 101)، والنتيجة «عرضي»
    hand_m = 0.3 ** 2 + (1 - 0.4) ** 2 + 0.3 ** 2
    hand_b = (33 / 101) ** 2 + (1 - 47 / 101) ** 2 + (21 / 101) ** 2
    check(abs(brier({"flat": .4, "up": .3, "down": .3}, "flat") - hand_m) < 1e-12 and abs(hand_b - 0.4358) < 1e-4,
          f"hand case: model {hand_m:.4f}, base rate {hand_b:.4f}")


def test_rules_and_lock(F, G):
    check(F.OUTLOOK_DAYS == 14 and F.OUTLOOK_BAND == 0.02, "OUTLOOK_DAYS = 14, OUTLOOK_BAND = 0.02")
    check(F.SC_RULES["fable"] == {"prelim_windows": 13, "min_windows": 26}
          and F.SC_RULES["fable_direction"] == {"prelim_windows": 13, "min_windows": 26}, "SC_RULES thresholds 13 and 26")
    lock = re.search(r"<!--lock:scoring=([0-9a-f]{12})-->", open("CONSTITUTION.md", encoding="utf-8").read()).group(1)
    fp = G.scoring_fingerprint(G.code_consts())
    check(fp == lock, f"scoring lock unchanged: {fp} == {lock}")


def fixtures(F, out):
    os.makedirs(out, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "data", "outlook_log.json"), F.OUTLOOK_LOG)
    tr = F.score_outlooks()
    cases = {"real": tr}
    n13, n26 = first_n_with_windows(F, 13), first_n_with_windows(F, 26)
    ds = session_days()[:n26]
    tail = next(d for i, d in enumerate(ds) if F._windows(ds[i:]) == 3)
    for name, log_ in (("n10", synthetic(10, varied=True)), ("prelim", synthetic(n13, varied=True)),
                       ("final", synthetic(varied=True)), ("nobase", synthetic(base=False)),
                       ("fewbase", synthetic(n26, varied=True, base_from=tail))):
        json.dump(log_, open(F.OUTLOOK_LOG, "w"))
        cases[name] = F.score_outlooks()
    old = json.loads(json.dumps(cases["final"]))      # ملخص بالشكل القديم: متوسطات بلا windows
    for k in ("windows", "windows_base", "windows_prelim", "windows_final", "brier_paired", "metrics_withheld"):
        old["summary"].pop(k, None)
    cases["old"] = old
    for name, t in cases.items():
        d = json.load(open("data.json"))
        d["outlook_track"] = t
        json.dump(d, open(os.path.join(out, f"data_{name}.json"), "w"), ensure_ascii=False)
        print(f"fixture data_{name}.json: scored {t['summary']['n']}, windows {t['summary'].get('windows')}, "
              f"with base rate {t['summary'].get('windows_base')}")


if __name__ == "__main__":
    out = sys.argv[sys.argv.index("--fixtures") + 1] if "--fixtures" in sys.argv else None
    if out:
        out = os.path.abspath(out)
    tmp, F, G = setup()
    try:
        test_windows_262(F)
        test_no_base_262(F)
        test_thresholds(F)
        test_few_base(F)
        test_small_samples(F)
        test_direction(F)
        test_scored_this_week_model(F)
        test_real_log(F)
        test_brier_base(F)
        test_rules_and_lock(F, G)
        if out:
            fixtures(F, out)
    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
