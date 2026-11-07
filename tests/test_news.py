# اختبارات فلتر «النفط والطاقة» في الأخبار اللحظية (المادة 22). تُشغَّل بـ: python tests/test_news.py
# تثبت: كلمات النفط تُلتقط وما يشبهها لا يُلتقط، ونمط Python ونمط الصفحة متطابقان حرفيًا (وكذلك فلتر الذهب)،
# وnews.json يحمل oil، ومدخل التقرير يعلّم عناوين النفط ويقدّمها في الأولوية فلا تسقط من أول 45 عنوانًا.
import datetime as dt
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fetch_gold as F  # noqa: E402

F.log = lambda *a, **k: None
fails = []


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg)
    if not ok:
        fails.append(msg)


# 1) الكلمات (ومن 7 نوفمبر: إيران وهرمز والمضيق والحرب والسلام، بطلب صاحب المنصة)
OIL_YES = ["Oil jumps 4% as tanker attacked near Hormuz", "Brent crude tops $110", "WTI falls after EIA inventory build",
           "OPEC+ agrees to cut output by 500k bpd", "Saudi Aramco raises OSP", "Houthis target ship in Red Sea",
           "US refinery outage lifts gasoline futures", "Diesel cracks widen", "US to tap SPR", "Baker Hughes rig count falls",
           "European natural gas tops EUR 100", "Qatar LNG cargo diverted", "US natgas storage injection", "Oil prices slide",
           "Shale producers trim capex", "Barrels stored at Cushing rise",
           "Iran says the Strait will stay open", "Strait of Hormuz reopens to tanker traffic", "Tehran rejects US proposal",
           "IRGC seizes vessel in Gulf of Oman", "US and Iran near peace deal", "Ceasefire holds in Gaza",
           "Russia-Ukraine peace talks resume", "Missile strike hits Kyiv", "Drone attack on Russian refinery",
           "Israel launches airstrikes on Iranian nuclear sites", "Navy blockade tightens", "War escalates in the Middle East",
           "Trade war fears hit crude demand"]
OIL_NO = ["Gold rallies on weaker dollar", "Turmoil in bond markets", "Fed's Powell speaks", "Treasury yields spread widens",
          "Soybean oilseed exports rise", "Spring payrolls beat", "Boiled frog strategy", "Analysts warn of slowdown",
          "Fed's Warsh speaks on balance sheet", "Trade war escalates as tariffs rise", "Price war among airlines",
          "Taiwan Strait drills unsettle markets", "Warner Bros shares jump"]
for t in OIL_YES:
    check(F.is_oil(t), f"oil matched: {t}")
for t in OIL_NO:
    check(not F.is_oil(t), f"oil NOT matched: {t}")

# 2) نمط الصفحة = نمط Python حرفيًا
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
for js, py in (("NEWS_OIL", F.FJ_OIL), ("NEWS_HOT", F.FJ_HOT), ("NEWS_OIL_SKIP", F.FJ_OIL_SKIP)):
    m = re.search(r"const " + js + r" = /(.+?)/g?i;", html)
    check(bool(m) and m.group(1) == py.pattern, f"index.html {js} is identical to fetch_gold.py pattern")
check("(?<" not in F.FJ_OIL.pattern + F.FJ_OIL_SKIP.pattern, "no lookbehind in the page patterns (Safari < 16.4 rejects it)")
check("data-news-f" in html and "'oil'" in html and "النفط والطاقة" in html, "index.html has the oil chip")

# 3) news.json يحمل oil للعناوين والبيانات
now = dt.datetime.now(dt.timezone.utc)
iso = lambda m: (now - dt.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
fj = {"news": {"1": {"newsId": 1, "datePublished": iso(5), "title": "Brent crude jumps after Hormuz tanker attack"},
               "2": {"newsId": 2, "datePublished": iso(6), "title": "Gold climbs as yields fall"},
               "3": {"newsId": 3, "datePublished": iso(7), "title": "Apple unveils new phone"}},
      "cal": {"9": {"id": 9, "timeUtc": iso(8), "title": "Crude Oil Inventories", "country": "US", "impact": 2,
                    "actual": "-2.1M", "forecast": "1.0M", "previous": "0.5M"}},
      "deleted": set(), "lines": 4, "last_recv": iso(1)}
tmp = tempfile.mkdtemp()
F.NEWS_OUT = os.path.join(tmp, "news.json")
os.environ.pop("ANTHROPIC_API_KEY", None)
F.publish_news(fj)
items = {x["id"]: x for x in json.load(open(F.NEWS_OUT))["items"]}
check(items["n1"]["oil"] is True and items["n2"]["oil"] is False and items["n3"]["oil"] is False, "news items carry oil flag")
check(items["n2"]["gold"] is True and items["n3"]["gold"] is False, "gold flag unchanged")
check(items["c9"]["oil"] is True and items["c9"]["kind"] == "data", "released data row (EIA crude) carries oil flag")

# 4) مدخل التقرير: oil معلَّم ومقدَّم في الأولوية
news = {str(i): {"newsId": i, "datePublished": iso(2 + i), "title": f"Company {i} reports earnings"} for i in range(60)}
news["900"] = {"newsId": 900, "datePublished": iso(600), "title": "OPEC+ surprises with deeper output cut"}
pack = F.fj_pack({"news": news, "cal": {}}, [], via_macro=True)
heads = pack["headlines_24h"]
oilh = [h for h in heads if "OPEC" in h["title"]]
check(len(heads) == 45, f"45 headlines kept ({len(heads)})")
check(len(oilh) == 1 and oilh[0].get("oil") is True, "older oil headline kept and marked oil=true despite 60 newer non-hot headlines")
check(not any(h.get("oil") for h in heads if "OPEC" not in h["title"]), "non-oil headlines carry no oil flag")
check("oil=true" in pack["note"], "wire note explains the oil flag")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
