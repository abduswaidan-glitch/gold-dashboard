"""اختبارات الفحص الآلي للتقرير، وجولة الإصلاح، والمراجعة اليدوية (المادة 15). بلا شبكة وبلا أي استدعاء API:
_claude_stream يُستبدل بنسخة وهمية تُرجع ردودًا مكتوبة سلفًا وتسجّل كل طلب. يحتاج requests لأن fetch_gold.py يستوردها.

تعمل على نسخة مؤقتة من المستودع ولا تكتب فيه شيئًا. التشغيل: python tests/test_validation.py
"""
import copy
import datetime as dt
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
    tmp = tempfile.mkdtemp(prefix="gold-validation-")
    for name in ("fetch_gold.py", "constitution_guard.py", "CONSTITUTION.md", "prices.json", "data.json"):
        shutil.copy(os.path.join(ROOT, name), tmp)
    for d in ("data", "prompts", ".github"):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d))
    os.chdir(tmp)
    sys.path.insert(0, tmp)
    import fetch_gold as F
    import constitution_guard as G
    F.log = lambda *a, **k: None
    F.correlation_pack = lambda d: None          # تجلب من الشبكة؛ لا علاقة لها بهذه الاختبارات
    F.nowcast_pack = lambda d: None
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    return tmp, F, G


def report(**over):
    """تقرير سليم بالشكل الذي يطلبه AI_SYSTEM."""
    r = {"audit": [{"check": c, "status": "ok", "note": "checked"} for c in "abcdefgh"],
         "outlook": {"bias": "neutral", "scenarios": [{"dir": "flat", "p": 45}, {"dir": "up", "p": 30}, {"dir": "down", "p": 25}]},
         "ar": {"headline": "الذهب في نطاق", "tone": "محايد", "summary": ["نقطة أولى", "نقطة ثانية", "نقطة ثالثة", "نقطة رابعة"],
                "sections": [{"title": f"قسم {i}", "body": "نص القسم"} for i in range(4)],
                "events": [{"title": "حدث", "impact": "أثر", "source": "https://www.federalreserve.gov/x", "date": "2026-11-05"}],
                "watch": ["أمر"], "risks": "مخاطر",
                "outlook": {"bias_note": "سبب", "scenarios": [{"title": "t", "why": "w", "trigger": "z"}] * 3, "flip": "f"}},
         "focus_zones": [{"zone": "Z1", "why": "سبب"}], "research_map": []}
    for k, v in over.items():
        r[k] = v
    return r


def codes(f, kind):
    return {c for c, _ in f[kind]}


def test_prompt(F, G):
    s = F.AI_SYSTEM
    check(all(x in s for x in ("1) Analyst", "2) Scenario architect", "3) Critic")), "AI_SYSTEM has the three passes in one answer")
    check(s.index('{"audit"') < s.index('"outlook": {"bias"'), "the JSON shape puts audit before outlook")
    check("never published" in s, "AI_SYSTEM says the audit is never published")
    for gone in ("which drivers are moving gold now", "say who is moving price", "the dollar driving it"):
        check(gone not in s, f"causal wording removed from AI_SYSTEM: «{gone}»")
    check("never as evidence of what caused a move" in s, "hidden correlations are for consistency only")
    src = open("fetch_gold.py", encoding="utf-8").read()
    for gone in ("not driving gold now", "yields and the dollar justify", "= gold-specific demand"):
        check(gone not in src, f"causal wording removed from correlation_pack: «{gone}»")
    fable = open("prompts/fable.md", encoding="utf-8").read()
    r10 = ("10. بعد الانتهاء أعد فحص و مراجعة الفرضيات و دقة التحليل و الانحياز و الشكل النهائي والتراجع عند الاكتشاف اخطاء "
           "و اعادة البحث و التدقيق وتجربة عدة طرق حل داخلياً قبل النشر")
    check(r10 in fable and "قائمة التدقيق (a إلى h)" in fable, "rule 10 kept word for word, with the checklist added")
    check("يتقدّم الأحدث تاريخًا، ثم الأكبر عيّنة" not in fable and "جودة المنهجية" in fable and "يقيسان الشيء نفسه" in fable,
          "rule 11: scope first, then methodology, sample, recency")
    check("ما تقوله الارتباطات المخفية بكلمات عامة" not in fable and "لفحص اتساق فرضيتك فقط" in fable,
          "rule 13: hidden correlations no longer count as causal evidence")
    for n in range(1, 16):
        check(re.search(rf"^{n}\.\s", fable, re.M) is not None, f"fable.md rule {n} still present")
    fp = F.prompt_fingerprint()
    check(re.fullmatch(r"[0-9a-f]{10}", fp) is not None and fp != "a2827d74aa",
          f"prompt fingerprint is a new version ({fp}, was a2827d74aa)")
    lock = re.search(r"<!--lock:scoring=([0-9a-f]{12})-->", open("CONSTITUTION.md", encoding="utf-8").read()).group(1)
    check(G.scoring_fingerprint(G.code_consts()) == lock == "f4c5f1b970b0", "scoring lock unchanged (f4c5f1b970b0)")
    check(F.SC_RULES["fable"] == {"prelim_windows": 13, "min_windows": 26} and F.OUTLOOK_DAYS == 14 and F.OUTLOOK_BAND == 0.02,
          "scoring thresholds, horizon and band unchanged")
    check(F.AI_MODEL == "claude-opus-5-5" and F.AI_EFFORT == "xhigh", "production model and effort unchanged (Opus 5.5, xhigh)")
    check(F.AI_REPAIR == "off", "AI_REPAIR defaults to off")


def test_by_prompt_separates_versions(F):
    px = dict((d, v) for d, v in json.load(open("prices.json"))["daily"] if v)
    days = [d for d in sorted(px) if "2026-01-02" <= d <= "2026-03-31"][:40]
    log_ = [{"id": d, "date": d, "model": "claude-opus-5-5", "prompt": "a2827d74aa" if i < 20 else F.prompt_fingerprint(),
             "bias": "neutral", "p": [40, 30, 30], "dirs": ["flat", "up", "down"], "base": {"date": d, "price": px[d]}}
            for i, d in enumerate(days)]
    json.dump(log_, open(F.OUTLOOK_LOG, "w"))
    bp = F.score_outlooks()["summary"]["by_prompt"]
    check(set(bp) == {"a2827d74aa", F.prompt_fingerprint()} and bp["a2827d74aa"]["n"] == 20
          and bp[F.prompt_fingerprint()]["n"] == 20, "by_prompt keeps the old and the new prompt versions apart (20 + 20)")


def test_validate(F):
    dig = {"statistical_direction_filter": {"zones": [{"id": "Z1"}, {"id": "Z2"}]}, "gold_trend_and_base_rates": {"close": 4057.6},
           "hidden_correlations": {"drivers": {"real_yield": {"r60": 0.37, "beta": -1.248}}, "fair_value_20d": {"model_r2": 0.412}},
           "hidden_nowcasts": {"cot": {"net_est": 12345}}}
    ok = report()
    f = F.validate_report(ok, F._outlook(ok), dig)
    check(not f["hard"], f"a clean report has no hard finding ({F._findings_line(f)})")
    for field, code in (("headline", "ar_shape"), ("summary", "ar_shape"), ("sections", "ar_shape")):
        r = report(); r["ar"][field] = [] if field != "headline" else ""
        check(code in codes(F.validate_report(r, F._outlook(r), dig), "hard"), f"missing {field} -> hard {code}")
    r = report(outlook={"bias": "neutral"})
    check("outlook_invalid" in codes(F.validate_report(r, F._outlook(r), dig), "hard"), "missing scenarios -> hard outlook_invalid")
    r = report(outlook={"bias": "neutral", "scenarios": [{"dir": "up", "p": 40}, {"dir": "up", "p": 30}, {"dir": "down", "p": 30}]})
    check("outlook_invalid" in codes(F.validate_report(r, F._outlook(r), dig), "hard"), "a direction listed twice -> hard outlook_invalid")
    r = report(outlook={"bias": "neutral", "scenarios": [{"dir": "flat", "p": 40}, {"dir": "up", "p": 30}, {"dir": "down", "p": 20}]})
    f = F.validate_report(r, F._outlook(r), dig)
    check(not f["hard"] and "p_sum" in codes(f, "soft"), "probabilities summing to 90 -> soft only (the code normalises them)")
    # 27 و29 أكتوبر: انحياز هابط والعرضي أعلى احتمالًا؛ مشروع بالقواعد الحالية، فلا يكون حاسمًا
    r = report(outlook={"bias": "bearish", "scenarios": [{"dir": "flat", "p": 45}, {"dir": "down", "p": 30}, {"dir": "up", "p": 25}]})
    f = F.validate_report(r, F._outlook(r), dig)
    check(not f["hard"] and "bias_not_top" in codes(f, "soft"), "bias differing from the top scenario -> soft only (as on 27 and 29 Oct)")
    for text, label in (("بيتا العائد الحقيقي -1.248", "a 4-digit beta"), ("صافي تقديري 12,345 عقد", "a nowcast of 12,345"),
                        ("القيمة ٠٫٤١٢", "Arabic-Indic digits ٠٫٤١٢"), ("مؤشر hidden_nowcasts", "a pack name")):
        r = report(); r["ar"]["sections"][0]["body"] = text
        check("hidden_leak" in codes(F.validate_report(r, F._outlook(r), dig), "hard"), f"hidden leak detected: {label}")
    r = report(); r["ar"]["sections"][0]["body"] = "تغيّر 0.37 في الجلسة"
    f = F.validate_report(r, F._outlook(r), dig)
    check(not f["hard"] and "hidden_maybe" in codes(f, "soft"), "a two-digit match (0.37) is soft, not hard (may be chance)")
    r = report(); r["ar"]["sections"][0]["body"] = "الإغلاق 4057.6"
    check("hidden_leak" not in codes(F.validate_report(r, F._outlook(r), dig), "hard"), "a public number is not a leak")
    msgs = " ".join(m for f2 in (F.validate_report(report(), None, dig),) for _, m in f2["hard"] + f2["soft"])
    check(not any(v in msgs for v in ("0.37", "1.248", "12345", "0.412")), "finding messages never carry a hidden value")
    # كل حقل يكتبه النموذج ويمكن أن يُحفظ، لا ar وحده
    for field, rep_over, q, label in (
            ("research_map", {"research_map": [{"section": "macro", "need": "n", "queries": ["q"], "sources": ["x.com"],
                                                 "found": "beta -1.248 vs yields"}]}, None, "a distinctive number in research_map"),
            ("research_map", {"research_map": [{"section": "macro", "need": "from hidden_correlations", "found": "x"}]}, None,
             "a pack name in research_map"),
            ("focus_zones", {"focus_zones": [{"zone": "Z1", "why": "r2 0.412 هنا"}]}, None, "a distinctive number in a focus reason"),
            ("queries", {}, ["gold nowcast 12,345 contracts"], "a distinctive number in a search query"),
            ("queries", {}, ["hidden_nowcasts cot"], "a pack name in a search query")):
        r = report(**rep_over)
        f = F.validate_report(r, F._outlook(r), dig, q)
        check("hidden_leak" in codes(f, "hard") and field in f["leak_fields"] and f["leak_fields"] == [field],
              f"leak detected in {field}: {label} (fields {f['leak_fields']})")
    r = report(audit=[{"check": "f", "status": "fixed", "note": "removed beta -1.248 and hidden_correlations"}])
    check(not F.validate_report(r, F._outlook(r), dig)["leak_fields"], "the audit itself is not scanned (it is never saved)")
    every = [F.validate_report(report(research_map=[{"found": "beta -1.248, net 12345, hidden_nowcasts"}],
                                      focus_zones=[{"zone": "Z1", "why": "0.412"}]), F._outlook(report()), dig, ["0.37 hidden_correlations"])]
    msgs = " ".join(m for f2 in every for _, m in f2["hard"] + f2["soft"])
    check(not any(v in msgs for v in ("1.248", "12345", "0.412", "0.37", "hidden_nowcasts", "hidden_correlations")),
          "no finding message carries a hidden value or a pack name, whatever the field")
    r = report(focus_zones=[{"zone": "4050", "why": "x"}, {"zone": "Z9", "why": "y"}])
    f = F.validate_report(r, F._outlook(r), dig)
    check(not f["hard"] and {"focus_not_id", "focus_unknown"} <= codes(f, "soft"), "bad focus zones -> soft (the code drops them)")
    r = report(); r.pop("audit")
    check("audit_missing" in codes(F.validate_report(r, F._outlook(r), dig), "soft"), "missing audit -> soft")
    r = report(audit=[{"check": "c", "status": "open", "note": "x"}])
    check("audit_open" in codes(F.validate_report(r, F._outlook(r), dig), "soft"), "an open audit item -> soft")


def test_history_false_positives(F):
    """التقارير الحقيقية المحفوظة: كم منها كان سيُعلَّم حاسمًا؟ (الحزم المخفية لتلك الأيام غير محفوظة، فلا يُقاس التسريب هنا)"""
    hist = json.load(open(os.path.join(ROOT, "data", "ai_history.json")))
    dig = json.load(open("data.json"))
    zones = {"statistical_direction_filter": dig.get("direction_filter") or {}}
    n, flagged = 0, []
    for h in hist:
        o = h.get("outlook") or {}
        if not o.get("dirs"):
            continue
        n += 1
        rep = {"ar": h.get("ar") or {}, "outlook": {"bias": o["bias"], "scenarios": [{"dir": d, "p": p} for d, p in zip(o["dirs"], o["p"])]},
               "focus_zones": [], "audit": [{"check": "a", "status": "ok"}]}
        f = F.validate_report(rep, F._outlook(rep), zones)
        if f["hard"]:
            flagged.append((h.get("date"), F._findings_line(f)))
    check(n > 0 and not flagged, f"{n} real past reports: no hard finding {flagged[:3]}")


class FakeAPI:
    """بديل _claude_stream: يُرجع الردود بالترتيب ويحفظ كل طلب، بلا أي اتصال."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, payload, key, beta=None, idle=300):
        self.calls.append(copy.deepcopy(payload))
        rep = self.replies.pop(0)
        extra = []
        if isinstance(rep, tuple):
            rep, extra = rep
        return {"model": payload["model"], "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, *extra,
                            {"type": "text", "text": json.dumps(rep, ensure_ascii=False)}],
                "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40,
                          "iterations": [{"type": "message", "input_tokens": 10, "output_tokens": 20}]}}


def published_files():
    out = {}
    for p in ("data/ai_history.json", "data/ai_research_log.json", "data/outlook_log.json"):
        out[p] = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    return out


def run_report(F, replies, repair):
    F.AI_REPAIR = repair
    fake = FakeAPI(replies)
    F._claude_stream = fake
    data = json.load(open("data.json"))
    prev = copy.deepcopy(data)
    before = published_files()
    try:
        out = F.ai_report(data, prev, {"daily": dt.date.today().isoformat()}, data.get("releases") or {})
        err = None
    except Exception as e:
        out, err = None, e
    return fake, out, err, before, published_files()


def test_repair(F):
    bad = report(outlook={"bias": "neutral"})              # لا سيناريوهات: فشل حاسم
    good = report()
    # off (الافتراضي): طلب واحد، والنشر كما كان قبل هذا التعديل
    fake, out, err, before, after = run_report(F, [bad], "off")
    check(err is None and len(fake.calls) == 1 and out and out[1] is True, "AI_REPAIR=off: one request, published as before")
    check(all('"audit"' not in v for v in after.values()), "AI_REPAIR=off: audit is not written to any published file")
    # on: فشل ثم نجاح = طلبان، والثاني إلحاق بالمحادثة نفسها
    fake, out, err, before, after = run_report(F, [bad, good], "on")
    a, b = fake.calls if len(fake.calls) == 2 else (None, None)
    check(err is None and len(fake.calls) == 2 and out and out[1] is True, "AI_REPAIR=on, fail then pass: exactly two requests, published")
    if a and b:
        check(b["messages"][:len(a["messages"])] == a["messages"] and b["messages"][-2]["role"] == "assistant"
              and b["messages"][-1]["role"] == "user" and "outlook_invalid" in b["messages"][-1]["content"],
              "the repair turn is appended to the same conversation (history unchanged) and names the failure")
        check(b["messages"][-2]["content"][0]["type"] == "thinking", "the thinking block is passed back unchanged")
        check(all(a[k] == b[k] for k in ("model", "system", "tools", "output_config")) and a.get("cache_control") == b.get("cache_control"),
              "same model, system, tools, effort and caching (the cached prefix can be read)")
    check(all('"audit"' not in v for v in after.values()), "after a repair: audit is not written to any published file")
    # on: فشل مرتين = طلبان فقط، والتقرير يُحجب، والملفات المنشورة لا تتغيّر
    fake, out, err, before, after = run_report(F, [bad, bad], "on")
    check(isinstance(err, F._AIWithheld) and len(fake.calls) == 2, "AI_REPAIR=on, fail twice: two requests, then withheld")
    check(before == after, "withheld: ai_history, research log and outlook_log are untouched")
    # on: نجاح من الأول = طلب واحد
    fake, out, err, before, after = run_report(F, [good], "on")
    check(err is None and len(fake.calls) == 1, "AI_REPAIR=on, pass first time: one request")
    # off: ولا تسريب حتى لو وُجد فشل حاسم؛ والفحص نفسه لا يُسقط التقرير إن تعطّل
    real = F.validate_report
    F.validate_report = lambda *a, **k: 1 / 0
    fake, out, err, before, after = run_report(F, [good], "on")
    F.validate_report = real
    check(err is None and len(fake.calls) == 1, "a crash inside the checks never blocks the report")
    check(F._withheld_today({"ai_withheld": {"date": "2026-11-06"}}, "2026-11-06")
          and not F._withheld_today({"ai_withheld": {"date": "2026-11-05"}}, "2026-11-06")
          and not F._withheld_today({}, "2026-11-06"), "a withheld report blocks retries for that New York day only")
    src = open("fetch_gold.py", encoding="utf-8").read()
    check(src.index("except _AIWithheld") < src.index('log("ai FAILED:"'), "main handles a withheld report before the generic retry path")
    F.AI_REPAIR = "off"


HIDDEN_CP = {"drivers": {"real_yield": {"r60": 0.37, "beta": -1.248}}, "fair_value_20d": {"model_r2": 0.412}}
HIDDEN_NC = {"cot": {"net_est": 12345}}
HIDDEN_STRINGS = ("1.248", "12345", "12,345", "0.412", "hidden_correlations", "hidden_nowcasts")


def capture_logs(F):
    lines = []
    F.log = lambda *a, **k: lines.append(" ".join(str(x) for x in a))
    return lines


def test_publication_leaks(F):
    """تقرير يسرّب في research_map ونصوص البحث وسبب منطقة: لا قيمة مخفية في أي ملف منشور ولا في سجل Actions."""
    F.correlation_pack = lambda d: copy.deepcopy(HIDDEN_CP)
    F.nowcast_pack = lambda d: copy.deepcopy(HIDDEN_NC)
    lines = capture_logs(F)
    leaky = report(research_map=[{"section": "macro", "need": "n", "queries": ["q"], "sources": ["x.com"],
                                  "found": "beta -1.248 from hidden_correlations"}],
                   focus_zones=[{"zone": "Z1", "why": "صافي 12,345 عقد"}])
    query = [{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "hidden_nowcasts cot 12345"}}]
    fake, out, err, before, after = run_report(F, [(leaky, query)], "off")
    check(err is None and len(fake.calls) == 1 and out and out[1] is True, "AI_REPAIR=off: one request, the report itself is published as before")
    pub = "\n".join(after.values()) + json.dumps(out[0] if out else {}, ensure_ascii=False)
    check(not any(v in pub for v in HIDDEN_STRINGS), "no hidden value or pack name in ai_history, the research log, outlook_log or the returned report")
    last = json.load(open("data/ai_research_log.json"))[-1]
    check(last.get("withheld") == ["focus_zones", "queries", "research_map"] and not last["research_map"] and not last["queries"],
          f"the research log withholds the leaking fields by name ({last.get('withheld')})")
    check("focus" not in (out[0]["latest"] if out else {}), "a leaking focus reason is not published (no star from it)")
    check(all('"audit"' not in v for v in after.values()), "audit is not saved anywhere")
    logs = "\n".join(lines)
    check(not any(v in logs for v in HIDDEN_STRINGS), "the Actions log carries no hidden value and no pack name")
    check("not saved because of private-pack content: focus_zones, queries, research_map" in logs, "the log names the withheld fields only")
    # تسريب في نص التقرير نفسه والإصلاح مطفأ: يُسجَّل حاسمًا، والسلوك كما وافق عليه صاحب المنصة (لا تعديل للنص)
    lines.clear()
    bad_ar = report(); bad_ar["ar"]["sections"][0]["body"] = "بيتا -1.248"
    fake, out, err, before, after = run_report(F, [bad_ar], "off")
    check(err is None and "hard: hidden_leak" in "\n".join(lines) and not any(v in "\n".join(lines) for v in HIDDEN_STRINGS),
          "a leak in ar with AI_REPAIR=off is logged as hard, without the value (the report text is not edited by code)")
    F.log = lambda *a, **k: None
    F.correlation_pack = lambda d: None
    F.nowcast_pack = lambda d: None


def snapshot(root, skip):
    import hashlib
    out = {}
    for d, _, files in os.walk(root):
        if os.path.abspath(d).startswith(os.path.abspath(skip)) or "__pycache__" in d:
            continue
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def test_review_isolation(F, tmp):
    """المراجعة اليدوية المعزولة: لا تغيّر أي ملف في المستودع، ولا تشغّل التحديث ولا التقرير اليومي، والنتيجة في out_dir وحده."""
    data = json.load(open("data.json"))
    data.setdefault("ai", {}).setdefault("latest", {})
    data["ai"]["latest"].update({"ar": report()["ar"], "outlook": {"bias": "neutral", "p": [45, 30, 25], "dirs": ["flat", "up", "down"]},
                                 "model": "claude-opus-5-5", "generated_utc": "2026-11-06T14:16:10+00:00"})
    json.dump(data, open("data.json", "w"), ensure_ascii=False)       # تقرير Opus منشور تقرؤه المراجعة (نسخة الاختبار فقط)
    out_dir = tempfile.mkdtemp(prefix="gold-review-out-")
    F.correlation_pack = lambda d: copy.deepcopy(HIDDEN_CP)
    F.nowcast_pack = lambda d: copy.deepcopy(HIDDEN_NC)
    calls = {"ai_report": 0, "main": 0}
    real_ai, real_main = F.ai_report, F.main
    F.ai_report = lambda *a, **k: calls.__setitem__("ai_report", calls["ai_report"] + 1)
    F.main = lambda *a, **k: calls.__setitem__("main", calls["main"] + 1)
    try:
        before = snapshot(tmp, out_dir)
        fake = FakeAPI([report()])
        F._claude_stream = fake
        got = F.review_main("claude-fable-5-1", out_dir)
        after = snapshot(tmp, out_dir)
        saved = json.load(open(got)) if got else {}
        check(len(fake.calls) == 1 and fake.calls[0]["model"] == "claude-fable-5-1", "the Fable review makes exactly one request, to claude-fable-5-1")
        check(calls == {"ai_report": 0, "main": 0}, "the review never runs the daily report or the full update")
        check(before == after, f"no repository file changes ({len(before)} files hashed before and after)")
        check(got and os.path.dirname(got) == out_dir and os.listdir(out_dir) == [os.path.basename(got)],
              "the only output is one file in the review directory (the artifact)")
        check(not ({"audit", "usage", "cost", "cost_30d"} & set(saved)) and saved.get("model") == "claude-fable-5-1",
              f"the review file has no audit, usage or cost keys ({sorted(saved)})")
        log_ = json.load(open(F.OUTLOOK_LOG))
        check(not any(e.get("model") == "claude-fable-5-1" and e.get("date") == saved.get("date") for e in log_),
              "the review is not in the official outlook log, so it is never scored")
        # رد مراجعة يسرّب: لا يُحفظ محتواه
        for f in os.listdir(out_dir):
            os.remove(os.path.join(out_dir, f))
        leaky = report(research_map=[{"found": "beta -1.248"}])
        F._claude_stream = FakeAPI([leaky])
        got = F.review_main("claude-fable-5-1", out_dir)
        txt = open(got, encoding="utf-8").read()
        check('"withheld"' in txt and not any(v in txt for v in HIDDEN_STRINGS), "a leaking review saves only the withheld field names")
        try:
            F.review_main("claude-opus-5-5", out_dir)
            check(False, "an unknown review model is refused")
        except SystemExit:
            check(True, "an unknown review model is refused")
    finally:
        F.ai_report, F.main = real_ai, real_main
        F.correlation_pack = lambda d: None
        F.nowcast_pack = lambda d: None
        shutil.rmtree(out_dir, ignore_errors=True)
    src = open("fetch_gold.py", encoding="utf-8").read()
    tail = src[src.index('if __name__ == "__main__":'):]
    check(tail.index('"--review" in sys.argv[1:]') < tail.index("main()"), "python fetch_gold.py --review routes to review_main, never to main()")


def test_review_workflow():
    wf = open(".github/workflows/review.yml", encoding="utf-8").read()
    on = wf[wf.index("\non:"):wf.index("\npermissions:")]
    check("workflow_dispatch" in on and not any(t in on for t in ("schedule", "push", "pull_request", "workflow_run")),
          "review.yml runs only when the owner presses Run workflow")
    check("contents: read" in wf and "contents: write" not in wf and "persist-credentials: false" in wf,
          "review.yml is read-only: contents: read, and git keeps no token")
    for bad in ("git push", "git commit", "run_loop", "gh workflow run", "update.yml", "AI_FORCE"):
        check(bad not in wf, f"review.yml never does «{bad}»")
    check("group: gold-review" in wf and "gold-data" not in wf, "review.yml uses its own concurrency group, not the live loop's")
    check("fetch_gold.py --review" in wf and "upload-artifact" in wf and "retention-days: 7" in wf,
          "review.yml runs the review entry point and keeps the result as a 7-day artifact")
    up = open(".github/workflows/update.yml", encoding="utf-8").read()
    check("fable" not in up, "the production update workflow has no Fable option")


def test_guard_private_key(G):
    sample = {"ai": {"latest": {"audit": [{"check": "a"}]}}}
    hits = [p for k, p in G.walk_keys(sample) if k in G.PRIVATE_KEYS]
    check(hits == ["/ai/latest/audit"], "the guard flags an audit key in a published file")


if __name__ == "__main__":
    tmp, F, G = setup()
    try:
        test_prompt(F, G)
        test_by_prompt_separates_versions(F)
        test_validate(F)
        test_history_false_positives(F)
        test_repair(F)
        test_publication_leaks(F)
        test_review_isolation(F, tmp)
        test_review_workflow()
        test_guard_private_key(G)
    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
