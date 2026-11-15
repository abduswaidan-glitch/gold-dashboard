#!/usr/bin/env python3
"""اختبارات فشل التقرير الذكي: الحالة تبقى ظاهرة حتى ينجح تقرير، وإعادة المحاولة في التحديث التالي بسقف يومي،
ولا نص خام من ردّ النموذج أو من الحزم المخفية في السجل ولا في الملفات المنشورة.

    python tests/test_report.py

يشغّل main() كاملًا في مجلد مؤقت على نسخة من ملفات المستودع الحقيقية، بساعة مزيّفة وردّ مزيّف يستبدل طلب
التقرير. لا يتصل بالشبكة ولا يرسل أي طلب مدفوع: كل طلب HTTP يُسجَّل ويفشل، والاختبار يتحقق أن عددها صفر.
"""
import copy
import datetime as real_dt
import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
UTC = real_dt.timezone.utc

# علامات لا يجوز أن تظهر في أي سجل أو ملف منشور
SECRET_OUT = "MODELRAW-7391-zeta"        # داخل نص ردّ النموذج
SECRET_HID = "HIDDENPACK-5521-kappa"     # داخل الحزمتين المخفيتين (للنموذج فقط)
SECRET_API = "APIBODY-8842-sigma"        # داخل جسم خطأ الخادم
SECRETS = (SECRET_OUT, SECRET_HID, SECRET_API)

# شكل ردّ 10 نوفمبر 2026: كائن قصير معطوب عند الحرف 97 ثم بداية جديدة في الرد نفسه
BROKEN = ('{"outlook":{"bias":"bearish","scenarios":[{"dir":"flat","p":38},{"dir":"down","p":34},{"dir":"up":"","p":0}]}}'
          '{"outlook":{"bias":"bearish"},"ar":{"summary":["' + SECRET_OUT + " " + SECRET_HID + '"]}}')
TRUNCATED = '{"outlook":{"bias":"neutral","scenarios":[{"dir":"flat","p":40}],"note":"' + SECRET_OUT
NO_AR = '{"outlook":{"bias":"neutral"},"note":"' + SECRET_OUT + '"}'

CLOCK = [None]
CALLS = []          # طلبات التقرير (المزيّفة) في التشغيل الحالي
NET = []            # أي طلب HTTP حقيقي حاول الكود إرساله
LOGS = []
BASE = [""]         # الملفات المنشورة قبل التشغيل: ما كان فيها أصلًا ليس تسريبًا


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


class FakeDT(real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        t = CLOCK[0]
        return t.astimezone(tz) if tz else t.replace(tzinfo=None)


class FakeDate(real_dt.date):
    @classmethod
    def today(cls):
        return CLOCK[0].date()


def fake_dt_module():
    m = types.ModuleType("datetime")
    for k in dir(real_dt):
        if not k.startswith("__"):
            setattr(m, k, getattr(real_dt, k))
    m.datetime, m.date = FakeDT, FakeDate
    return m


def at(day, h, m, s=1, us=574608):
    """لحظة UTC في نوفمبر 2026. نيويورك = UTC-5 (توقيت شتوي)، فموعد التقرير 9:00 نيويورك = 14:00 UTC."""
    return FakeDT(2026, 11, day, h, m, s, us, tzinfo=UTC)


def setup():
    tmp = tempfile.mkdtemp(prefix="gold-report-")
    for name in ("fetch_gold.py", "constitution_guard.py", "CONSTITUTION.md"):
        shutil.copy(os.path.join(ROOT, name), tmp)
    os.chdir(tmp)
    sys.path.insert(0, tmp)
    for k in ("AI_FORCE", "FULL_REFRESH", "EXPERIMENT_MODEL", "AI_MODEL", "AI_EFFORT", "FJ_API_KEY"):
        os.environ.pop(k, None)
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"      # مفتاح وهمي: لا طلب يخرج من هذا الاختبار
    import requests

    def no_network(self, method, url, *a, **k):
        NET.append(f"{method} {url}")
        raise RuntimeError("no network in tests")
    requests.sessions.Session.request = no_network
    import fetch_gold as F
    import constitution_guard as G
    F.dt = G.dt = fake_dt_module()
    F.log = lambda *a: LOGS.append(" ".join(str(x) for x in a))
    F.is_due = lambda *a, **k: False                 # لا جلب لأي مصدر: الاختبار عن مسار التقرير وحده
    F.fj_read = lambda: None
    F.correlation_pack = lambda d: {"note": SECRET_HID}
    F.nowcast_pack = lambda d: {"note": SECRET_HID}

    def offline(*a, **k):
        raise RuntimeError("offline")
    F.macro_wire = F._claude = offline
    return tmp, F, G


def fresh(tmp, state_ai_fail=None):
    """يعيد ملفات البيانات إلى نسخة المستودع، كي يبدأ كل سيناريو من الحالة نفسها."""
    for name in ("data.json", "prices.json", "status.json"):
        shutil.copy(os.path.join(ROOT, name), tmp)
    for d in ("data", "prompts"):
        shutil.rmtree(os.path.join(tmp, d), ignore_errors=True)
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d))
    if state_ai_fail is not None:
        p = os.path.join(tmp, "data", "state.json")
        st = json.load(open(p, encoding="utf-8"))
        st["ai_fail"] = state_ai_fail
        json.dump(st, open(p, "w", encoding="utf-8"), ensure_ascii=False)


def msg(text, stop="end_turn"):
    return {"model": "claude-opus-5-5", "stop_reason": stop, "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def good_text():
    """تقرير سليم مبني من آخر تقرير حقيقي محفوظ في data.json."""
    last = json.load(open(os.path.join(ROOT, "data.json"), encoding="utf-8"))["ai"]["latest"]
    return json.dumps({"outlook": {"bias": "neutral", "scenarios": [{"dir": "flat", "p": 40}, {"dir": "up", "p": 30},
                                                                   {"dir": "down", "p": 30}]},
                       "ar": copy.deepcopy(last["ar"]), "research_map": []}, ensure_ascii=False)


def run(F, t, reply=None, force=False):
    """تشغيل main() واحد عند اللحظة t. reply: ما يعيده طلب التقرير (رسالة، أو استثناء يُرفع)."""
    CLOCK[0] = t
    del CALLS[:], LOGS[:]
    if force:
        os.environ["AI_FORCE"] = "true"
    else:
        os.environ.pop("AI_FORCE", None)

    def stream(payload, key, beta=None, idle=300):
        CALLS.append(1)
        if reply is None:
            raise AssertionError("unexpected report request")
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)
    F._claude_stream = stream
    BASE[0] = "\n".join(open(p, encoding="utf-8").read() for p in PUBLISHED)
    F.main()
    status = json.load(open("status.json", encoding="utf-8"))
    state = json.load(open(os.path.join("data", "state.json"), encoding="utf-8"))
    return (status.get("sources") or {}).get("ai") or {}, state, len(CALLS)


PUBLISHED = ("status.json", "data.json", os.path.join("data", "state.json"))


def published():
    """كل ما يُكتب في ملفات تُرفع إلى المستودع العام، مع سجل التشغيل."""
    return "\n".join(LOGS) + "\n" + "\n".join(open(p, encoding="utf-8").read() for p in PUBLISHED)


def leaked(text, raw):
    """هل ظهر في text أحد الأسرار، أو مقطع من 24 حرفًا من الرد الخام لم يكن في الملفات المنشورة قبل التشغيل؟"""
    if any(s in text for s in SECRETS):
        return True
    wins = (raw[i:i + 24] for i in range(0, max(1, len(raw) - 24), 8)) if raw else ()
    return any(w in text and w not in BASE[0] for w in wins)


# ---------------------------------------------------------------- السيناريوهات
def test_cycle(tmp, F, G):
    """دورة الحالات كاملة: فشل، انتظار، محاولتان إضافيتان، بلوغ السقف، ثم نجاح في اليوم التالي."""
    fresh(tmp)
    last_ok = json.load(open("data.json", encoding="utf-8"))["ai"]["latest"]["generated_utc"]
    ai, st, n = run(F, at(16, 14, 7), msg(BROKEN))
    check(n == 1 and ai.get("ok") is False, "cycle: the first attempt is made and recorded as failed")
    check(ai.get("state") == "retry_pending", f"cycle: state after the first failure is retry_pending (got {ai.get('state')})")
    check(ai.get("attempts") == 1 and ai.get("max_attempts") == 3, f"cycle: attempt 1 of 3 (got {ai.get('attempts')}/{ai.get('max_attempts')})")
    check(ai.get("last_ok") == last_ok, f"cycle: last_ok keeps the last successful report ({last_ok}; got {ai.get('last_ok')})")
    check(ai.get("kind") == "parse", f"cycle: failure kind is parse (got {ai.get('kind')})")
    ra = ai.get("retry_after") or ""
    check("2026-11-16T14:27" <= ra < "2026-11-16T14:37:00", f"cycle: next try is due before the next half-hour update (got {ra!r})")
    since = ai.get("failed_since")
    check(bool(since) and since[:16] == "2026-11-16T14:07", f"cycle: failed_since is the first failure (got {since!r})")

    ai, st, n = run(F, at(16, 14, 20))
    check(n == 0, "wait: no request while waiting for the retry time")
    check(ai.get("ok") is False and ai.get("state") == "retry_pending",
          f"wait: the failure stays in status.json on a run that does not retry (got {ai or 'no ai entry'})")
    check(ai.get("failed_since") == since and ai.get("last_ok") == last_ok and not ai.get("skipped"),
          "wait: same failed_since and last_ok, and not marked as skipped")

    ai, st, n = run(F, at(16, 14, 37, 0, 900000), msg(BROKEN))
    check(n == 1, f"retry: the next half-hour update retries, even when it starts a second early (requests: {n})")
    check(ai.get("attempts") == 2 and ai.get("state") == "retry_pending", f"retry: attempt 2 of 3, still retry_pending (got {ai.get('attempts')}, {ai.get('state')})")
    check(ai.get("failed_since") == since, "retry: failed_since does not move on a later failure")

    ai, st, n = run(F, at(16, 15, 7), msg(BROKEN))
    check(n == 1 and ai.get("attempts") == 3, f"cap: the second extra attempt is made (requests: {n}, attempts: {ai.get('attempts')})")
    check(ai.get("state") == "report_failed" and not ai.get("retry_after"),
          f"cap: after two extra failures the state is report_failed with no retry time (got {ai.get('state')}, {ai.get('retry_after')})")

    ai, st, n = run(F, at(16, 15, 37))
    check(n == 0 and ai.get("state") == "report_failed", f"cap: no fourth request the same day, and the failure stays visible (requests: {n}, state: {ai.get('state')})")
    ai, st, n = run(F, at(16, 16, 7), msg(good_text()), force=True)
    check(n == 0 and ai.get("state") == "report_failed", f"cap: AI_FORCE does not override the daily cap (requests: {n})")
    ai, st, n = run(F, at(17, 3, 7))
    check(n == 0 and ai.get("state") == "report_failed", f"cap: the day is New York's: 03:07 UTC next day is still the same day (requests: {n})")

    ai, st, n = run(F, at(17, 14, 7), msg(good_text()))
    check(n == 1, f"next day: the daily report is requested again (requests: {n})")
    check(ai.get("ok") is True and "ai_fail" not in st, f"next day: a success clears the failure (ok={ai.get('ok')}, ai_fail in state: {'ai_fail' in st})")
    latest = json.load(open("data.json", encoding="utf-8"))["ai"]["latest"]
    check(latest.get("date") == "2026-11-17", f"next day: the new report is published (date {latest.get('date')})")
    ai, st, n = run(F, at(17, 14, 37))
    check(n == 0 and ai.get("ok", True), "next day: nothing more after the success")


def test_recover_same_day(tmp, F):
    fresh(tmp)
    run(F, at(16, 14, 7), msg(BROKEN))
    ai, st, n = run(F, at(16, 14, 37), msg(good_text()))
    check(n == 1 and ai.get("ok") is True and "ai_fail" not in st, "recover: a retry that succeeds clears the failure")
    latest = json.load(open("data.json", encoding="utf-8"))["ai"]["latest"]
    check(latest.get("date") == "2026-11-16", f"recover: today's report is published (date {latest.get('date')})")


def test_force(tmp, F):
    """التشغيل اليدوي: يتجاوز مهلة الانتظار، ولا يتجاوز السقف، وفشله يُحسب."""
    fresh(tmp)
    run(F, at(16, 14, 7), msg(BROKEN))
    ai, st, n = run(F, at(16, 14, 12), msg(good_text()), force=True)
    check(n == 1 and ai.get("ok") is True, f"force: a manual run during the wait writes the report (requests: {n})")
    fresh(tmp)
    run(F, at(16, 14, 7), msg(good_text()))
    ai, st, n = run(F, at(16, 15, 0), msg(BROKEN), force=True)
    today = json.load(open("data.json", encoding="utf-8"))["ai"]["latest"]["generated_utc"]
    check(n == 1 and ai.get("state") == "report_failed" and ai.get("last_ok") == today,
          f"force: a manual failure after today's report is recorded with no retry, last_ok = today's report (state {ai.get('state')})")
    run(F, at(16, 15, 10), msg(BROKEN), force=True)
    ai, st, n = run(F, at(16, 15, 20), msg(BROKEN), force=True)
    check(n == 1 and ai.get("attempts") == 3, f"force: manual failures count toward the daily cap (attempts {ai.get('attempts')})")
    ai, st, n = run(F, at(16, 15, 30), msg(good_text()), force=True)
    check(n == 0, f"force: no manual request beyond the cap (requests: {n})")


def test_legacy_state(tmp, F):
    """حالة فشل بالصيغة القديمة (مهلة 6 ساعات، بلا يوم): لا تحجب المحاولة بعد الدمج."""
    fresh(tmp, {"at": "2026-11-16T14:07:01.574608+00:00", "error": "old", "retry_after": "2026-11-16T20:07:01.574608+00:00"})
    ai, st, n = run(F, at(16, 14, 37), msg(good_text()))
    check(n == 1 and ai.get("ok") is True, f"legacy: an old-format failure does not block the next update (requests: {n})")


def test_kinds_and_leaks(tmp, F):
    """كل نوع فشل يُصنَّف، ولا يتسرّب شيء من الرد الخام أو الحزم المخفية أو جسم خطأ الخادم."""
    import requests

    def crash(*a, **k):
        raise KeyError(SECRET_OUT)
    api_body = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": SECRET_API}})
    bad_req = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": SECRET_API}})
    cases = [
        ("parse", msg(BROKEN), BROKEN, None),
        ("parse", msg(TRUNCATED, "max_tokens"), TRUNCATED, None),
        ("shape", msg(NO_AR), NO_AR, None),
        ("refusal", msg(SECRET_OUT, "refusal"), "", None),
        ("http", F.ClaudeHTTPError(529, api_body), api_body, None),
        ("http", F.ClaudeHTTPError(400, bad_req), bad_req, None),
        ("network", requests.ConnectionError(SECRET_API), "", None),
        ("other", msg(good_text()), "", crash),
    ]
    orig = F._event_dates
    for kind, reply, raw, patch in cases:
        fresh(tmp)
        F._event_dates = patch or orig
        ai, st, n = run(F, at(16, 14, 7), reply)
        F._event_dates = orig
        tag = f"{kind}{' (' + reply['stop_reason'] + ')' if isinstance(reply, dict) else ' (' + str(getattr(reply, 'status', '')) + ')'}"
        check(ai.get("ok") is False and ai.get("kind") == kind, f"kinds: {tag} is classified as {kind} (got {ai.get('kind')})")
        check(ai.get("attempts") == 1 and ai.get("state") == "retry_pending", f"kinds: {tag} counts as one attempt and waits for a retry")
        check(not leaked(published(), raw), f"leak: {tag}: no raw reply, hidden pack or server body in the log, status.json, state.json or data.json")
    fresh(tmp)
    ai, st, n = run(F, at(16, 14, 7), msg(BROKEN))
    err = str(ai.get("error") or "")
    check("97" in err and str(len(BROKEN)) in err, f"detail: the error names the position (97) and the reply length ({len(BROKEN)}): {err!r}")
    check("sha" in err and BROKEN[:12] not in err, "detail: the error carries a fingerprint, not the reply's first characters")
    check(len(err) <= 140, f"detail: the error fits the page's 140-character line ({len(err)})")
    fresh(tmp)
    ai, st, n = run(F, at(16, 14, 7), msg(TRUNCATED, "max_tokens"))
    check("max_tokens" in str(ai.get("error")), f"detail: a cut-off reply is told apart by its stop reason: {ai.get('error')!r}")


def test_guard(tmp, F, G):
    """الحارس يعرف أن التقرير فشل وينتظر إعادة المحاولة، بلا ادعاء أنه لم ينجح قط؛ والمخالفة بعد 24 ساعة من بدء الفشل."""
    fresh(tmp)
    run(F, at(16, 14, 7), msg(BROKEN))
    rows, hard = G.faults()
    check("`ai`" in rows, "guard: the failed report is listed in the running faults")
    check("لم ينجح قط" not in rows, "guard: no false claim that the report never succeeded")
    check("13 نوفمبر" in rows, f"guard: the fault names the last successful report (13 نوفمبر): {rows[:160]!r}")
    check(hard == [], f"guard: a report waiting for its retry is not a 24-hour violation (hard: {hard})")
    check(not leaked(rows, BROKEN), "guard: the fault line carries no raw reply")
    run(F, at(16, 14, 37), msg(BROKEN))
    run(F, at(16, 15, 7), msg(BROKEN))
    CLOCK[0] = at(16, 20, 0)
    rows, hard = G.faults()
    check("`ai`" in rows and hard == [], f"guard: a capped failure the same day is a listed fault, not yet a violation (hard: {hard})")
    CLOCK[0] = at(17, 14, 30)
    rows, hard = G.faults()
    check(hard == ["ai"], f"guard: failing for more than 24 hours since the first failure is a violation (hard: {hard})")
    C = G.code_consts()
    lock = G.LOCK.search(G.rd(G.CONST, ""))
    check(bool(lock) and lock.group(1) == G.scoring_fingerprint(C), "guard: the scoring lock is untouched")


def test_schema(tmp, F):
    """الطلب يحمل مخطط التقرير، وإن رفضه الخادم يُعاد الطلب نفسه بدونه، والتقرير يُكتب في الحالتين."""
    def walk(n, path="$"):
        bad = []
        if n.get("type") == "object":
            if n.get("additionalProperties") is not False or sorted(n.get("required") or []) != sorted(n["properties"]):
                bad.append(path)
            for k, v in n["properties"].items():
                bad += walk(v, f"{path}.{k}")
        elif n.get("type") == "array":
            bad += walk(n["items"], path + "[]")
        return bad
    S = F.AI_REPORT_SCHEMA
    check(walk(S) == [], f"schema: every object lists all its fields as required and allows no extra field ({walk(S)})")
    check(set(S["properties"]) == {"outlook", "ar", "focus_zones", "research_map"}, "schema: the four top-level fields of the prompt's shape")
    check(all(('"' + k + '"') in F.AI_SYSTEM for k in ("headline", "tone", "summary", "sections", "events", "watch", "risks", "bias_note", "flip")),
          "schema: its report fields are the ones the prompt names")
    seen = []

    def run_with(reject):
        fresh(tmp)
        CLOCK[0] = at(16, 14, 7)
        del LOGS[:], seen[:]
        os.environ.pop("AI_FORCE", None)

        def stream(payload, key, beta=None, idle=300):
            fmt = (payload.get("output_config") or {}).get("format")
            seen.append(bool(fmt) and fmt.get("type") == "json_schema" and fmt.get("schema") == S)
            if reject and fmt:
                raise F.ClaudeHTTPError(400, json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": SECRET_API}}))
            return copy.deepcopy(msg(good_text()))
        F._claude_stream = stream
        F.main()
        return (json.load(open("status.json", encoding="utf-8")).get("sources") or {}).get("ai") or {}
    ai = run_with(False)
    check(seen == [True] and ai.get("ok") is True, f"schema: the report request carries the schema, one request (sent: {seen})")
    check(any("structured output on" in l for l in LOGS), "schema: the log says structured output is on")
    check(json.load(open("data.json", encoding="utf-8"))["ai"]["latest"].get("structured") is True, "schema: the report records that the schema was enforced")
    ai = run_with(True)
    check(seen == [True, False] and ai.get("ok") is True, f"schema: when the server rejects the schema, the same request is retried without it and the report is written (sent: {seen})")
    check(any("structured output off" in l for l in LOGS) and not any(SECRET_API in l for l in LOGS),
          "schema: the log says structured output is off, without the server's message")
    check(json.load(open("data.json", encoding="utf-8"))["ai"]["latest"].get("structured") is False, "schema: the report records that it was written as free text")


def main():
    tmp, F, G = setup()
    try:
        test_cycle(tmp, F, G)
        test_recover_same_day(tmp, F)
        test_force(tmp, F)
        test_legacy_state(tmp, F)
        test_kinds_and_leaks(tmp, F)
        test_guard(tmp, F, G)
        test_schema(tmp, F)
        check(NET == [], f"network: no HTTP request left the test ({len(NET)} attempted: {NET[:3]})")
    finally:
        os.chdir(ROOT)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
