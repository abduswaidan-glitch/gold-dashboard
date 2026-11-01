#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_gold.py
يجلب بيانات الذهب من المصادر الرسمية ويكتبها في data.json.
يعمل تلقائيًا عبر GitHub Actions، ولا يحتاج إلى أي تعديل.

المصادر:
  IMF IRFCL   حيازات الذهب الرسمية لكل دولة (شهري)
  CFTC COT    مراكز Swap Dealers و Producer/Merchant في عقود COMEX Gold (أسبوعي)
  CME         مخزون مستودعات COMEX: Registered و Eligible (يومي)
  SPDR GLD    الذهب داخل صندوق GLD (يومي)

كل الكميات تُحفظ بالكيلوغرام. إذا فشل مصدر، تبقى آخر بياناته الناجحة كما هي.
"""
import bisect
import csv
import datetime as dt
import io
import json
import math
import os
import re
import statistics
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

OZ_KG = 0.0311034768           # troy ounce -> kg
CONTRACT_OZ = 100              # COMEX GC = 100 oz
MAX_COUNTRY_KG = 9_000_000     # سقف منطقي: أكبر حائز (الولايات المتحدة) ~8,133 طن
START_MONTH = "2022-01"
START_DAY = "2022-01-01"
START_DAY_PRICES = "2008-01-01"

OUT = "data.json"
STATUS_OUT = "status.json"              # يتغير كل ساعة: حالة الفحص فقط
PRICES_OUT = "prices.json"              # سعر الذهب: كل ساعة
COMEX_HIST = os.path.join("data", "comex_history.json")
DELIV_HIST = os.path.join("data", "deliveries_history.json")
F13_HIST = os.path.join("data", "f13_history.json")
GLD_OZPS = os.path.join("data", "gld_ozps.json")
ETF_HIST = os.path.join("data", "etf_history.json")
SGE_HIST = os.path.join("data", "sge_history.json")
GLD_LBMA = os.path.join("data", "gld_lbma_pm.json")
STATE = os.path.join("data", "state.json")
RELEASES = os.path.join("data", "releases.json")
WGC_HIST = os.path.join("data", "wgc_etf.json")
GEX_HIST = os.path.join("data", "gex_history.json")
F13_TYPES = os.path.join("data", "f13_types.json")
OUTLOOK_LOG = os.path.join("data", "outlook_log.json")

IMF_URL = "https://api.imf.org/external/sdmx/2.1/data/IRFCL/.IRFCLDT1_IRFCL56V_FTO..M"
COT_URLS = [
    "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",   # Disaggregated, Futures only
    "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json",   # Disaggregated, Futures + Options
]
GOLD_CODE = "088691"
LEGACY_URLS = [
    "https://publicreporting.cftc.gov/resource/6dca-aqww.json",   # Legacy, Futures only
    "https://publicreporting.cftc.gov/resource/jun7-fc8e.json",   # Legacy, Futures + Options
]
CME_URL = "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls"
CME_ISSUES_URL = "https://www.cmegroup.com/delivery_reports/MetalsIssuesAndStopsReport.pdf"
CME_ISSUES_MTD_URL = "https://www.cmegroup.com/delivery_reports/MetalsIssuesAndStopsMTDReport.pdf"
GLD_PAGE = "https://www.spdrgoldshares.com/usa/historical-data/"
GLD_PAGES = [GLD_PAGE, "https://www.spdrgoldshares.com/usa/"]
GLD_CANDIDATES = [
    "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en",
    "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.xlsx",
    "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv",
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 90


# ---------------------------------------------------------------- helpers
def log(*a):
    print("[gold]", *a, flush=True)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def get(url, **kw):
    h = dict(HEADERS)
    h.update(kw.pop("headers", {}))
    r = requests.get(url, headers=h, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def ensure(module, package=None):
    """يستورد مكتبة، ويثبّتها تلقائيًا إن لم تكن موجودة."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        import subprocess
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", package or module], check=False)
        return importlib.import_module(module)


def browser_get(url, headers=None, allow_html=False):
    """جلب يحاكي متصفح Chrome (بصمة TLS) لتجاوز حجب Akamai/Cloudflare لخوادم GitHub."""
    errors = []
    try:
        creq = ensure("curl_cffi.requests", "curl_cffi")
        for imp in ("chrome", "safari", "edge"):
            try:
                r = creq.get(url, impersonate=imp, headers=headers or {}, timeout=TIMEOUT)
                if r.status_code == 200 and (allow_html or not looks_like_html(r.content)):
                    return r
                errors.append(f"{imp}: HTTP {r.status_code} {page_hint(r.content)}")
            except Exception as e:
                errors.append(f"{imp}: {e}")
    except Exception as e:
        errors.append(f"curl_cffi: {e}")
    try:
        r = requests.get(url, headers={**HEADERS, **(headers or {})}, timeout=TIMEOUT)
        if r.status_code == 200 and (allow_html or not looks_like_html(r.content)):
            return r
        errors.append(f"requests: HTTP {r.status_code} {page_hint(r.content)}")
    except Exception as e:
        errors.append(f"requests: {e}")
    raise RuntimeError(" | ".join(errors)[:280])


def looks_like_html(content):
    head = content[:400].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head" in head


def page_hint(content):
    m = re.search(rb"<title>(.*?)</title>", content[:3000], re.I | re.S)
    return ("[" + m.group(1).decode("utf-8", "replace").strip()[:60] + "]") if m else ""


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def period_to_date(p):
    """'2026-M08' / '2026-08' -> '2026-08-01'"""
    p = str(p or "").strip()
    m = re.match(r"^(\d{4})-M?(\d{1,2})$", p)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1).isoformat()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", p)
    if m:
        return p[:10]
    return None


def parse_day(s):
    s = str(s or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def value_at(series, date):
    """آخر قيمة بتاريخ <= date في سلسلة مرتبة [[date, v], ...]"""
    dates = [d for d, _ in series]
    i = bisect.bisect_right(dates, date) - 1
    return series[i][1] if i >= 0 else None


def upsert(series, date, value):
    for row in series:
        if row[0] == date:
            row[1] = value
            break
    else:
        series.append([date, value])
    series.sort(key=lambda r: r[0])


# ---------------------------------------------------------------- IMF
def _tag(el):
    return el.tag.split("}")[-1]


def imf_rows(text):
    """يقرأ ردّ SDMX بصيغة CSV أو XML ويعيد (country, period, value)."""
    t = text.lstrip("\ufeff").lstrip()
    rows = []
    if t.startswith("<"):
        root = ET.fromstring(t.encode("utf-8"))
        for s in root.iter():
            if _tag(s) != "Series":
                continue
            key = dict(s.attrib)
            for v in s.iter():                      # generic SDMX: SeriesKey/Value
                if _tag(v) == "Value" and v.get("id") and v.get("id") not in key:
                    key[v.get("id")] = v.get("value")
            area = key.get("COUNTRY") or key.get("REF_AREA")
            for o in s:
                if _tag(o) != "Obs":
                    continue
                per, val = o.get("TIME_PERIOD"), o.get("OBS_VALUE")
                if per is None or val is None:
                    for c in o:
                        if _tag(c) == "ObsDimension":
                            per = c.get("value")
                        elif _tag(c) == "ObsValue":
                            val = c.get("value")
                rows.append((area, per, val))
    else:
        for r in csv.DictReader(io.StringIO(t)):
            area = r.get("COUNTRY") or r.get("REF_AREA")
            rows.append((area, r.get("TIME_PERIOD"), r.get("OBS_VALUE")))
    return rows


def fetch_imf():
    rows, errors = [], []
    for accept in ("application/vnd.sdmx.data+csv;version=1.0.0",
                   "application/vnd.sdmx.structurespecificdata+xml;version=2.1",
                   "application/xml"):
        try:
            r = get(IMF_URL, params={"startPeriod": START_MONTH}, headers={"Accept": accept})
            rows = imf_rows(r.text)
            if rows:
                break
            errors.append(f"{accept}: empty")
        except Exception as e:
            errors.append(f"{accept}: {e}")
            log("IMF", accept, e)
    if not rows:
        raise RuntimeError("IMF: no data — " + (errors[-1] if errors else "")[:200])

    raw, rejected = {}, []
    for area, per, val in rows:
        if not area or not re.fullmatch(r"[A-Z]{3}", str(area)):
            continue
        d, v = period_to_date(per), num(val)
        if not d or v is None or v < 0 or d < START_MONTH:
            continue
        kg = v * OZ_KG
        if kg > MAX_COUNTRY_KG:                      # خطأ وحدات في بيانات الدولة
            rejected.append(f"{area}:{d}")
            continue
        raw.setdefault(area, {})[d] = kg

    countries = {}
    for area, s in raw.items():
        med = statistics.median(s.values())
        clean = {}
        for d, kg in s.items():
            # قفزة 20 ضعفًا عن الوسيط = خطأ مقياس وليس شراءً حقيقيًا
            if med > 1000 and (kg > med * 20 or kg < med / 20):
                rejected.append(f"{area}:{d}")
                continue
            clean[d] = round(kg)
        if clean:
            countries[area] = sorted([d, v] for d, v in clean.items())

    if not countries:
        raise RuntimeError("IMF: parsed 0 countries")
    if rejected:
        log(f"IMF rejected {len(rejected)} implausible values:", ", ".join(rejected[:20]))

    # الإجمالي العالمي: نستعمل الدول المتوفرة منذ بداية النافذة لتجنّب قفزات مصطنعة
    months = sorted({d for s in countries.values() for d, _ in s})
    first = months[0]
    first_plus = (dt.date.fromisoformat(first) + dt.timedelta(days=95)).isoformat()
    core = {c: s for c, s in countries.items() if s[0][0] <= first_plus}
    count = {m: 0 for m in months}
    for s in core.values():
        for d, _ in s:
            count[d] += 1
    maxc = max(count.values())
    complete = [m for m in months if count[m] >= 0.8 * maxc]
    last = complete[-1]
    total = []
    for m in months:
        if m > last:
            break
        total.append([m, round(sum(value_at(s, m) or 0 for s in core.values()))])

    log(f"IMF ok: {len(countries)} countries, total as of {last}")
    return {
        "countries": countries,
        "central_banks": {
            "series": total,
            "as_of": last,
            "reporting": count[last],
            "in_total": len(core),
        },
    }


# ---------------------------------------------------------------- CFTC COT
AVOID = ("change", "pct", "traders", "conc", "old", "other")


def find_key(row, must, avoid=AVOID):
    keys = [k for k in row
            if all(m in k.lower() for m in must) and not any(a in k.lower() for a in avoid)]
    keys.sort(key=len)
    return keys[0] if keys else None


def socrata_gold(urls, limit=170):
    params = {
        "$where": f"cftc_contract_market_code='{GOLD_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(limit),
    }
    errors = []
    for url in urls:
        try:
            rows = get(url, params=params, headers={"Accept": "application/json"}).json()
            if rows:
                return rows
            errors.append(f"{url.rsplit('/', 1)[-1]}: empty")
        except Exception as e:
            errors.append(f"{url.rsplit('/', 1)[-1]}: {e}")
            log("COT", url, e)
    raise RuntimeError("no data — " + " | ".join(errors)[:220])


def cot_records(rows, keys):
    """[[date, v1, v2, ...]] بالعقود، مرتبة تصاعديًا."""
    out = {}
    for r in rows:
        d = str(r.get("report_date_as_yyyy_mm_dd", ""))[:10]
        vals = [num(r.get(k)) if k else 0.0 for k in keys]
        if d and all(v is not None for v in vals):
            out[d] = [d] + [int(v) for v in vals]
    return [out[d] for d in sorted(out)]


def fetch_cot():
    rows = socrata_gold(COT_URLS, limit=700)
    s = rows[0]
    k = {
        "sl": find_key(s, ("swap", "positions", "long")),
        "ss": find_key(s, ("swap", "positions", "short")),
        "sp": find_key(s, ("swap", "positions", "spread")),
        "pl": find_key(s, ("prod_merc", "positions", "long")),
        "ps": find_key(s, ("prod_merc", "positions", "short")),
        "ml": find_key(s, ("m_money", "positions", "long")),
        "ms": find_key(s, ("m_money", "positions", "short")),
        "msp": find_key(s, ("m_money", "positions", "spread")),
    }
    if not all(k[x] for x in ("sl", "ss", "pl", "ps")):
        raise RuntimeError(f"COT: fields not found {k}")
    swap = cot_records(rows, [k["sl"], k["ss"], k["sp"]])          # [d, long, short, spread]
    prod = cot_records(rows, [k["pl"], k["ps"], None])
    mm = cot_records(rows, [k["ml"], k["ms"], k["msp"]]) if k["ml"] and k["ms"] else []
    if not swap:
        raise RuntimeError("COT: parsed 0 rows")
    to_kg = CONTRACT_OZ * OZ_KG
    last = swap[-1]
    banks = {
        "swap_net": [[r[0], round((r[1] - r[2]) * to_kg)] for r in swap],
        "prod_net": [[r[0], round((r[1] - r[2]) * to_kg)] for r in prod],
        "latest": {"date": last[0], "swap_long": round(last[1] * to_kg), "swap_short": round(last[2] * to_kg),
                   "prod_long": round(prod[-1][1] * to_kg), "prod_short": round(prod[-1][2] * to_kg)},
    }
    log(f"COT ok: {len(swap)} weeks, latest {last[0]}")
    return {"banks": banks, "cot_disagg": {"swap": swap, "prod": prod, "mm": mm}}


def fetch_cot_legacy():
    rows = socrata_gold(LEGACY_URLS, limit=700)
    s = rows[0]
    k = [
        find_key(s, ("noncomm", "long")),
        find_key(s, ("noncomm", "short")),
        find_key(s, ("noncomm", "spread")),
        find_key(s, ("open_interest",)),
        find_key(s, ("comm", "long"), AVOID + ("noncomm",)),
        find_key(s, ("comm", "short"), AVOID + ("noncomm",)),
        find_key(s, ("nonrept", "long")),
        find_key(s, ("nonrept", "short")),
    ]
    if not all(k[:3]):
        raise RuntimeError(f"COT legacy: fields not found {k[:3]} in {list(s)[:12]}")
    recs = cot_records(rows, k)     # [d, long, short, spread, oi, comm_l, comm_s, nonrept_l, nonrept_s]
    if not recs:
        raise RuntimeError("COT legacy: parsed 0 rows")
    log(f"COT non-commercial ok: {len(recs)} weeks, latest {recs[-1][0]}")
    out = {"noncomm": recs}
    try:
        out["cot_extra"] = legacy_extra(rows)
    except Exception as e:
        log("COT concentration/traders FAILED:", str(e)[:200])
    try:
        fo = socrata_gold(LEGACY_URLS[1:], limit=160)
        ko = find_key(fo[0], ("open_interest",))
        out["oi_fo"] = [[r[0], r[1]] for r in cot_records(fo, [ko])] if ko else []
    except Exception as e:
        log("COT futures+options OI FAILED:", str(e)[:200])
    return out


EXTRA_AVOID = ("change", "pct", "old", "other")
EXTRA_FIELDS = (("traders_total", ("traders", "tot")), ("traders_nc_long", ("traders", "noncomm", "long")),
                ("traders_nc_short", ("traders", "noncomm", "short")), ("traders_c_long", ("traders", "comm", "long")),
                ("traders_c_short", ("traders", "comm", "short")),
                ("g4_long", ("conc", "gross", "4", "long")), ("g4_short", ("conc", "gross", "4", "short")),
                ("g8_long", ("conc", "gross", "8", "long")), ("g8_short", ("conc", "gross", "8", "short")),
                ("n4_long", ("conc", "net", "4", "long")), ("n4_short", ("conc", "net", "4", "short")),
                ("n8_long", ("conc", "net", "8", "long")), ("n8_short", ("conc", "net", "8", "short")))


def legacy_extra(rows):
    """من تقرير Legacy نفسه: عدد المتداولين في كل فئة، وتركّز المراكز لدى أكبر 4 و8 متداولين (٪ من Open Interest)."""
    s = rows[0]
    keys = {}
    for name, must in EXTRA_FIELDS:
        avoid = EXTRA_AVOID + (("noncomm", "rept") if name.startswith("traders_c_") else ("rept",) if name == "traders_total" else ())
        keys[name] = find_key(s, must, avoid)
    if not keys["g4_long"] or not keys["traders_total"]:
        raise RuntimeError(f"fields not found: {[n for n, k in keys.items() if not k]}")
    out = {}
    for r in rows:
        d = str(r.get("report_date_as_yyyy_mm_dd", ""))[:10]
        if d:
            out[d] = [d] + [num(r.get(keys[n])) if keys[n] else None for n, _ in EXTRA_FIELDS]
    return {"columns": [n for n, _ in EXTRA_FIELDS], "rows": [out[d] for d in sorted(out)][-160:]}


# ---------------------------------------------------------------- daily open interest (CME, best effort)
OI_DAILY = os.path.join("data", "oi_daily.json")
CME_VOL_URL = "https://www.cmegroup.com/CmeWS/mvc/Volume/Details/F/437/{}/P"      # 437 = COMEX Gold (GC)


def fetch_oi_daily():
    """الـ Open Interest اليومي لعقود GC من نشرة CME اليومية؛ أسرع من تقرير COT الأسبوعي بثلاثة أيام."""
    hist = load(OI_DAILY, [])
    have = {h[0] for h in hist}
    errors, added = [], 0
    day = dt.date.today()
    # السجل القصير يُكمَّل حتى ~25 جلسة (لقراءة 15 جلسة)، ثم يكفي آخر أسبوع؛ وحدّ للطلبات في التشغيل الواحد
    depth, asked = (40 if len(hist) < 20 else 8), 0
    for back in range(1, depth):
        d = day - dt.timedelta(days=back)
        if d.weekday() > 4 or d.isoformat() in have or asked >= 30:
            continue
        asked += 1
        try:
            j = browser_get(CME_VOL_URL.format(d.strftime("%Y%m%d")), headers={"Accept": "application/json"}).json()
            t = j.get("totals") or {}
            oi = num(t.get("atClose") or t.get("openInterest"))
            vol = num(t.get("totalVolume"))
            if oi:
                hist.append([d.isoformat(), int(oi), int(vol or 0)])
                added += 1
        except Exception as e:
            errors.append(f"{d}: {str(e)[:80]}")
    hist = sorted({h[0]: h for h in hist}.values())[-260:]
    if not hist:
        raise RuntimeError("CME daily OI: " + (" | ".join(errors) or "no data")[:240])
    save(OI_DAILY, hist)
    log(f"CME daily OI ok: {len(hist)} days, latest {hist[-1]}" + (f" ({len(errors)} days failed)" if errors else ""))
    return {"oi_daily": hist[-120:]}


# ---------------------------------------------------------------- CME COMEX
def fetch_comex():
    import xlrd
    r = browser_get(CME_URL, headers={
        "Referer": "https://www.cmegroup.com/clearing/operations-and-deliveries/nymex-delivery-notices.html",
    })
    sh = xlrd.open_workbook(file_contents=r.content).sheet_by_index(0)

    def row_text(i):
        return " ".join(str(c) for c in sh.row_values(i) if isinstance(c, str)).upper()

    report_date = None
    for label in ("ACTIVITY DATE", "REPORT DATE", "DATE"):
        for i in range(min(sh.nrows, 25)):
            t = row_text(i)
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
            if label in t and m:
                report_date = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
                break
        if report_date:
            break
    report_date = report_date or dt.date.today().isoformat()

    vals, deps, current = {}, {}, None
    for i in range(sh.nrows):
        row = sh.row_values(i)
        strs = [c.strip() for c in row if isinstance(c, str) and c.strip()]
        t = re.sub(r"\s+", " ", " ".join(strs).upper()).strip()
        nums = [c for c in row if isinstance(c, float)]
        if not nums:
            # سطر يحوي نصًا واحدًا فقط = اسم مستودع (عناوين الأعمدة تحوي عدة نصوص)
            if (len(strs) == 1 and len(t) > 3 and not re.search(r"\d{1,2}/\d{1,2}/\d{4}", t)
                    and not any(w in t for w in ("TROY", "OUNCE", "WAREHOUSE STOCKS", "COMEX", "DISCLAIMER", "INFORMATION"))):
                current = strs[0]
            continue
        if "TOTAL REGISTERED" in t:
            vals["registered"] = nums[-1]
        elif "TOTAL ELIGIBLE" in t:
            vals["eligible"] = nums[-1]
        elif "COMBINED TOTAL" in t:
            vals["total"] = nums[-1]
        elif current and "REGISTERED" in t and "TOTAL" not in t:
            deps.setdefault(current, {})["registered"] = nums[-1]
        elif current and "ELIGIBLE" in t and "TOTAL" not in t:
            deps.setdefault(current, {})["eligible"] = nums[-1]
    if "registered" not in vals or "eligible" not in vals:
        raise RuntimeError(f"CME: totals not found in sheet (found {list(vals)})")
    vals.setdefault("total", vals["registered"] + vals["eligible"])

    hist = load(COMEX_HIST, {"registered": [], "eligible": [], "total": []})
    for name in ("registered", "eligible", "total"):
        upsert(hist.setdefault(name, []), report_date, round(vals[name] * OZ_KG))
    dh = hist.setdefault("depositories", {})
    for name, v in deps.items():
        reg, eli = v.get("registered", 0.0), v.get("eligible", 0.0)
        series = dh.setdefault(name, [])
        series[:] = [x for x in series if x[0] != report_date] + [[report_date, round(reg * OZ_KG), round(eli * OZ_KG)]]
        series.sort()
    save(COMEX_HIST, hist)
    log(f"COMEX ok: {report_date}, total {vals['total']:,.0f} oz, {len(deps)} depositories")


# ---------------------------------------------------------------- CME Issues & Stops (per bank)
def _is_num(w):
    return re.fullmatch(r"[\d,]+", w) is not None


def parse_issues_pdf(content):
    """يقرأ تقرير Issues and Stops ويعيد {date, firms: {name: {H:[issued, stopped], C:[...]}}} لعقود الذهب."""
    pdfplumber = ensure("pdfplumber")
    report_date, firms, gold_sections = None, {}, 0
    in_gold, cols = False, None
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            lines = {}
            for w in words:
                lines.setdefault(round(w["top"] / 3), []).append(w)
            for key in sorted(lines):
                ws = sorted(lines[key], key=lambda w: w["x0"])
                toks = [w["text"] for w in ws]
                text = " ".join(toks).upper()
                if report_date is None:
                    m = re.search(r"BUSINESS DATE:?\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
                    if m:
                        report_date = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
                if "CONTRACT:" in text:
                    in_gold = ("GOLD" in text and not any(x in text for x in ("MICRO", "KILO", "MINI", "SILVER")))
                    gold_sections += in_gold
                    cols = None
                    continue
                if "ISSUED" in text and "STOPPED" in text:
                    cols = {w["text"].upper(): (w["x0"] + w["x1"]) / 2 for w in ws if w["text"].upper() in ("ISSUED", "STOPPED")}
                    continue
                if not in_gold or len(toks) < 3:
                    continue
                if not (re.fullmatch(r"\d{3}", toks[0]) and toks[1].upper() in ("H", "C")):
                    continue
                j = len(ws)
                while j > 2 and _is_num(ws[j - 1]["text"]):
                    j -= 1
                name = " ".join(toks[2:j]).strip().upper()
                if not name:
                    continue
                iss = stp = 0
                for w in ws[j:]:
                    v = int(w["text"].replace(",", ""))
                    xc = (w["x0"] + w["x1"]) / 2
                    if cols and "ISSUED" in cols and "STOPPED" in cols:
                        if abs(xc - cols["ISSUED"]) <= abs(xc - cols["STOPPED"]):
                            iss += v
                        else:
                            stp += v
                    else:
                        stp += v
                rec = firms.setdefault(name, {}).setdefault(toks[1].upper(), [0, 0])
                rec[0] += iss
                rec[1] += stp
    return {"date": report_date, "firms": firms, "gold_sections": gold_sections}


def fetch_deliveries():
    hist = load(DELIV_HIST, {"daily": {}, "mtd": {}})
    ref = {"Referer": "https://www.cmegroup.com/clearing/operations-and-deliveries/nymex-delivery-notices.html"}
    daily = parse_issues_pdf(browser_get(CME_ISSUES_URL, headers=ref).content)
    if not daily["date"]:
        raise RuntimeError("Issues&Stops: business date not found in PDF")
    hist["daily"][daily["date"]] = daily["firms"]
    for d in sorted(hist["daily"])[:-400]:
        del hist["daily"][d]
    msg = f"{daily['date']}: {len(daily['firms'])} firms in {daily['gold_sections']} gold sections"
    try:
        mtd = parse_issues_pdf(browser_get(CME_ISSUES_MTD_URL, headers=ref).content)
        if mtd["firms"]:
            hist["mtd"] = {"date": mtd["date"] or daily["date"], "firms": mtd["firms"]}
            msg += f"; MTD {len(mtd['firms'])} firms"
    except Exception as e:
        log("Issues&Stops MTD skipped:", e)
    save(DELIV_HIST, hist)
    log("Issues&Stops ok:", msg)


# ---------------------------------------------------------------- SPDR GLD
def table_sheets(content):
    """يحوّل ملف XLSX أو CSV إلى قائمة أوراق: [(اسم الورقة, صفوف)]."""
    head = content[:8]
    if head.startswith(b"%PDF"):
        raise RuntimeError("file is a PDF, not data")
    if head.startswith(b"PK"):                       # xlsx
        openpyxl = ensure("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
    text = content.decode("utf-8-sig", errors="replace")
    return [("csv", list(csv.reader(text.splitlines())))]


def parse_gld_sheets(sheets, save_ozps=True):
    errors = []
    for name, rows in sheets:
        try:
            return parse_gld_rows(rows, save_ozps)
        except Exception as e:
            errors.append(f"[{name}] {e}")
    raise RuntimeError(" ; ".join(errors)[:240])


def cell_day(v):
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return parse_day(v)


def parse_gld_rows(rows, save_ozps=True):
    def low(r):
        return [str(c or "").strip().lower() for c in r]
    hi = next((i for i, r in enumerate(rows[:300])
               if any("date" in c for c in low(r)) and any(("tonnes" in c or "ounces" in c) for c in low(r))), None)
    if hi is None:
        dated = [str(c)[:40] for r in rows[:300] for c in r if c and "date" in str(c).lower()][:3]
        raise RuntimeError(f"header not found ({len(rows)} rows; date cells: {dated})")
    header = low(rows[hi])
    di = next(i for i, h in enumerate(header) if "date" in h)
    tcols = [i for i, h in enumerate(header) if "tonnes" in h]
    ocols = [i for i, h in enumerate(header) if "ounces" in h]
    if tcols:
        ci, factor = next((i for i in tcols if "trust" in header[i]), tcols[0]), 1000.0
    else:
        ci, factor = next((i for i in ocols if "trust" in header[i]), ocols[0]), OZ_KG
    hn = [re.sub(r"\s+", " ", h) for h in header]
    gi = next((i for i, h in enumerate(hn) if "total" not in h and (
        "in gold" in h or ("per share" in h and ("ounce" in h or "gold" in h)) or ("ounces of gold" in h and "share" in h))), None)  # ذهب لكل سهم
    ni = next((i for i, h in enumerate(hn) if "nav" in h and ("share" in h or "per gld" in h)
               and "in gold" not in h and "total" not in h), None)                                   # قيمة السهم بالدولار
    li = next((i for i, h in enumerate(hn) if "lbma" in h or "gold price" in h), None)              # سعر LBMA إن وُجد صراحة
    ci_close = next((i for i, h in enumerate(hn) if "close" in h or "closing" in h), None)
    toz_i = next((i for i, h in enumerate(hn) if "total" in h and "ounce" in h), None)              # إجمالي الأونصات
    tnav_i = next((i for i, h in enumerate(hn) if "total net asset value" in h and "ounce" not in h and "tonne" not in h), None)
    if save_ozps:
        log("GLD archive columns:", " | ".join(f"{i}:{h[:38]}" for i, h in enumerate(hn))[:600])
    out, ozps, lbma = {}, {}, {}
    for r in rows[hi + 1:]:
        if len(r) <= max(di, ci):
            continue
        d, v = cell_day(r[di]), num(r[ci])
        if d and gi is not None and len(r) > gi and d >= "2015-01-01":
            g = num(r[gi])
            oz = g / 1000 if g and 50 < g < 110 else (g if g and 0.05 < g < 0.11 else None)   # بالألف أونصة أو بالأونصة
            if oz:
                ozps[d[:7]] = round(oz, 6)                                                     # آخر قيمة في كل شهر
                if d >= "2023-01-01":
                    px = num(r[li]) if li is not None and len(r) > li else None
                    if not (px and 500 < px < 30000):
                        nav = num(r[ni]) if ni is not None and len(r) > ni else None
                        px = nav / oz if nav else None                                        # سعر LBMA PM الذي يُقيَّم به GLD
                    if not (px and 500 < px < 30000) and toz_i is not None and tnav_i is not None and len(r) > max(toz_i, tnav_i):
                        toz, tnav = num(r[toz_i]), num(r[tnav_i])
                        px = tnav / toz if toz and tnav else None                             # إجمالي قيمة الصندوق ÷ إجمالي أونصاته
                    if not (px and 500 < px < 30000) and ci_close is not None and len(r) > ci_close:
                        cl = num(r[ci_close])
                        px = cl / oz if cl else None                                          # بديل أخير: سعر إغلاق السهم
                    if px and 500 < px < 30000:
                        lbma[d] = round(px, 2)
        if d and v and d >= START_DAY:
            out[d] = round(v * factor)
    if not out:
        raise RuntimeError(f"parsed 0 rows (columns: {header[:5]})")
    if ozps and save_ozps:
        save(GLD_OZPS, ozps)
    if save_ozps:
        log(f"GLD-derived London price: {len(lbma)} days; gold-per-share col={gi}, NAV col={ni}, LBMA col={li}"
            + (f", latest {max(lbma)} = ${lbma[max(lbma)]:,.2f}" if lbma else ""))
        if lbma:
            save(GLD_LBMA, lbma)
    return sorted([d, v] for d, v in out.items())


def find_archive_links(page_url, html):
    """يلتقط روابط ملفات xlsx/csv من الصفحة، بما فيها الروابط المخبّأة داخل JSON."""
    from urllib.parse import urljoin
    text = (html.decode("utf-8", "replace").replace("\\/", "/")
            .replace("\\u002F", "/").replace("&amp;", "&"))
    found = []
    for m in re.finditer(r"""(?:https?:)?//[^\s"'<>()]+?\.(?:xlsx|csv)(?:\?[^\s"'<>()]*)?"""
                         r"""|https?://[^\s"'<>()]*historical-archive[^\s"'<>()]*"""
                         r"""|(?<=["'(])/[^\s"'<>()]+?\.(?:xlsx|csv)(?:\?[^\s"'<>()]*)?""", text, re.I):
        u = m.group(0)
        u = ("https:" + u) if u.startswith("//") else urljoin(page_url, u)
        low = u.lower()
        if "gldm" in low or u in found:
            continue
        found.append(u)
    # الأرجح أولًا: ما يحمل كلمة archive أو historical أو اسم GLD
    found.sort(key=lambda u: -sum(k in u.lower() for k in ("archive", "histor", "gld")))
    return found


def fetch_gld():
    errors, urls = [], []
    for page_url in GLD_PAGES:
        try:
            html = browser_get(page_url, allow_html=True).content
            links = find_archive_links(page_url, html)
            log(f"GLD links on {page_url}: {links[:5] or 'none'}")
            if not links:
                i = html.lower().find(b"archive")
                if i >= 0:
                    log("GLD page context:", html[max(0, i - 300):i + 300].decode("utf-8", "replace").replace("\n", " "))
            urls += [u for u in links if u not in urls]
        except Exception as e:
            errors.append(f"page: {e}")
    urls += [u for u in GLD_CANDIDATES if u not in urls]
    i = 0
    while i < min(len(urls), 10):
        u = urls[i]
        i += 1
        try:
            content = browser_get(u, headers={"Referer": GLD_PAGE}).content
            if content.lstrip()[:1] in (b"{", b"["):        # API أعاد JSON يحوي رابط الملف
                inner = find_archive_links(u, content)
                inner += re.findall(r'https?://[^\s"]+', content.decode("utf-8", "replace").replace("\\/", "/"))
                urls[i:i] = [x for x in inner if x not in urls][:3]
                raise RuntimeError("JSON response, following file link")
            series = parse_gld_sheets(table_sheets(content))
            log(f"GLD ok: {len(series)} days, latest {series[-1][0]} ({u})")
            return {"etf": {"GLD": series}}
        except Exception as e:
            errors.append(f"{u.rsplit('/', 1)[-1][:50]}: {e}")
    raise RuntimeError("GLD: " + " | ".join(errors)[:260])


# ---------------------------------------------------------------- more gold ETFs
GLDM_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gldm&exchange=NYSE&lang=en"
IAU_PAGE = "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund"


def fetch_etfs():
    """GLDM (أرشيف SPDR كامل) و IAU (صفحة iShares اليومية، يُبنى سجلها يوميًا)."""
    hist = load(ETF_HIST, {})
    out, errors = {}, []
    try:
        out["GLDM"] = parse_gld_sheets(table_sheets(browser_get(GLDM_URL, headers={"Referer": GLD_PAGE}).content), save_ozps=False)
        log(f"GLDM ok: {len(out['GLDM'])} days, latest {out['GLDM'][-1][0]}")
    except Exception as e:
        errors.append(f"GLDM: {e}")
        log("GLDM FAILED:", e)
    try:
        html = browser_get(IAU_PAGE, allow_html=True).content.decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        m = re.search(r"Tonnes in Trust\s*([\d,]+\.?\d*)\s*as of\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})", text)
        if not m:
            m2 = re.search(r"Ounces in Trust\s*([\d,]+\.?\d*)\s*as of\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})", text)
            if not m2:
                raise RuntimeError("IAU: 'Tonnes in Trust' not found on page")
            kg, d = num(m2.group(1)) * OZ_KG, m2.group(2)
        else:
            kg, d = num(m.group(1)) * 1000, m.group(2)
        day = dt.datetime.strptime(re.sub(r"\s+", " ", d.replace(",", ", ")).replace(" ,", ","), "%b %d, %Y").date().isoformat()
        series = hist.setdefault("IAU", [])
        upsert(series, day, round(kg))
        out["IAU"] = series
        log(f"IAU ok: {day}, {kg / 1000:,.2f} t ({len(series)} days in history)")
    except Exception as e:
        errors.append(f"IAU: {e}")
        log("IAU FAILED:", e)
        if hist.get("IAU"):
            out["IAU"] = hist["IAU"]
    save(ETF_HIST, hist)
    if not out:
        raise RuntimeError(" | ".join(errors)[:280])
    return {"etf2": out, "etf2_errors": errors}


# ---------------------------------------------------------------- physical market: EFP & Shanghai premium
LBMA_PM = "https://prices.lbma.org.uk/json/gold_pm.json"
LBMA_AM = "https://prices.lbma.org.uk/json/gold_am.json"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=2y&interval=1d"
YAHOO2 = "https://query2.finance.yahoo.com/v8/finance/chart/{}?range=2y&interval=1d"
SGE_API = "https://www.sge.com.cn/graph/DayilyJzj"
SGE_HOME = "https://www.sge.com.cn/"
FX_URL = "https://api.frankfurter.app/{}..?from=USD&to=CNY"


def lbma_series(url):
    rows = browser_get(url, headers={"Accept": "application/json"}).json()
    out = {}
    for r in rows:
        d, v = r.get("d"), (r.get("v") or [None])[0]
        if d and v:
            out[d[:10]] = float(v)
    return out


def yahoo_series(symbol):
    errors = []
    for base in (YAHOO, YAHOO2):
        try:
            j = browser_get(base.format(symbol), headers={"Accept": "application/json"}).json()
            res = j["chart"]["result"][0]
            ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
            return {dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(): float(c) for t, c in zip(ts, cl) if c}
        except Exception as e:
            errors.append(str(e)[:80])
    raise RuntimeError(f"Yahoo {symbol}: " + " | ".join(errors))


def sge_series():
    hist = load(SGE_HIST, {})
    try:
        creq = ensure("curl_cffi.requests", "curl_cffi")
        r = creq.post(SGE_API, impersonate="chrome", timeout=TIMEOUT,
                      data={"start": (dt.date.today() - dt.timedelta(days=760)).isoformat(), "end": dt.date.today().isoformat()},
                      headers={"Referer": "https://www.sge.com.cn/sjzx/jzj", "X-Requested-With": "XMLHttpRequest"})
        j = r.json()
        for key in ("wp", "zp"):
            for ts, v in j.get(key, []):
                d = dt.datetime.fromtimestamp(ts / 1000 + 8 * 3600, dt.timezone.utc).date().isoformat()
                if v:
                    hist.setdefault(d, {})["pm" if key == "wp" else "am"] = float(v)
        log(f"SGE API ok: {len(hist)} days")
    except Exception as e:
        log("SGE API failed, trying homepage:", str(e)[:120])
        html = browser_get(SGE_HOME, allow_html=True).content.decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        md = re.search(r"行情日期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
        am = re.search(r"上海金早盘价[^\d]*([\d.]+)", text)
        pm = re.search(r"上海金午盘价[^\d]*([\d.]+)", text)
        if not (md and (am or pm)):
            raise RuntimeError("SGE: benchmark not found on homepage")
        rec = hist.setdefault(md.group(1), {})
        if am:
            rec["am"] = float(am.group(1))
        if pm:
            rec["pm"] = float(pm.group(1))
        log(f"SGE homepage ok: {md.group(1)}")
    for d in sorted(hist)[:-800]:
        del hist[d]
    save(SGE_HIST, hist)
    return {d: v.get("pm") or v.get("am") for d, v in hist.items() if v.get("pm") or v.get("am")}


def fx_usdcny():
    start = (dt.date.today() - dt.timedelta(days=760)).isoformat()
    try:
        j = get(FX_URL.format(start)).json()
        return {d: v["CNY"] for d, v in j.get("rates", {}).items() if v.get("CNY")}
    except Exception as e:
        log("FX frankfurter failed, trying Yahoo:", str(e)[:100])
        return yahoo_series("CNY=X")


def value_on_or_before(table, d, max_days=4):
    for k in range(max_days + 1):
        x = (dt.date.fromisoformat(d) - dt.timedelta(days=k)).isoformat()
        if x in table:
            return table[x]
    return None


def fetch_physical():
    errors, out = [], {}
    try:
        pm = lbma_series(LBMA_PM)
        am = lbma_series(LBMA_AM)
        out["london_src"] = "LBMA"
    except Exception as e:
        # LBMA تحجب خوادم GitHub (Cloudflare). البديل: سعر LBMA PM المستخرج من أرشيف GLD الرسمي،
        # لأن الصندوق يُقيَّم يوميًا بهذا السعر بالضبط: السعر = NAV للسهم ÷ الذهب في السهم.
        log("LBMA blocked, using LBMA PM derived from the GLD archive:", str(e)[:90])
        pm = load(GLD_LBMA, {})
        am = pm
        out["london_src"] = "GLD"
        if not pm:
            raise RuntimeError(f"London price unavailable (LBMA: {str(e)[:120]}; GLD-derived: empty)")
    cut = (dt.date.today() - dt.timedelta(days=730)).isoformat()
    try:
        gc = yahoo_series("GC=F")
        efp = []
        for d in sorted(gc):
            if d >= cut and d in pm:
                efp.append([d, round(gc[d] - pm[d], 2), round(gc[d], 2), round(pm[d], 2)])
        out["efp"] = efp
        log(f"EFP ok: {len(efp)} days, latest {efp[-1][0] if efp else '-'}")
    except Exception as e:
        errors.append(f"EFP: {e}")
        log("EFP FAILED:", e)
    try:
        sge = sge_series()
        fx = fx_usdcny()
        prem = []
        for d in sorted(sge):
            ref = am.get(d) or pm.get(d)
            rate = value_on_or_before(fx, d)
            if d >= cut and ref and rate:
                usd = sge[d] * 31.1034768 / rate
                prem.append([d, round(usd - ref, 2), round(usd, 2), round(ref, 2)])
        out["sh"] = prem
        log(f"Shanghai premium ok: {len(prem)} days, latest {prem[-1][0] if prem else '-'}")
    except Exception as e:
        errors.append(f"SGE: {e}")
        log("Shanghai premium FAILED:", e)
    if not out.get("efp") and not out.get("sh"):
        raise RuntimeError(" | ".join(errors)[:280])
    out["errors"] = errors
    log(f"Physical: London price source = {out['london_src']}")
    return {"physical": out}


# ---------------------------------------------------------------- gold price (hourly)
def yahoo_chart(symbol, rng, interval, period=None, ohlc=False):
    errors = []
    span = f"period1={period[0]}&period2={period[1]}" if period else f"range={rng}"
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?{span}&interval={interval}"
        try:
            res = browser_get(url, headers={"Accept": "application/json"}).json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            if ohlc:
                return res.get("meta", {}), [(t, float(o or c), float(h), float(lo), float(c)) for t, o, h, lo, c in
                                             zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"]) if c and h and lo]
            return res.get("meta", {}), [(t, float(c)) for t, c in zip(res["timestamp"], q["close"]) if c]
        except Exception as e:
            errors.append(f"{host}: {str(e)[:80]}")
    raise RuntimeError(f"Yahoo {symbol} {interval}: " + " | ".join(errors))


def _day(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()


def yahoo_daily(symbol, start):
    """إغلاقات يومية فعلية من تاريخ معيّن: range الطويل يعيد نقاطًا شهرية، فنطلب الفترة بتواريخ صريحة على دفعات."""
    a0 = int(dt.datetime.combine(start, dt.time(), dt.timezone.utc).timestamp())
    end = int(dt.datetime.now(dt.timezone.utc).timestamp())
    out = {}
    for a in range(a0, end, 1500 * 86400):
        _, rows = yahoo_chart(symbol, None, "1d", (a, min(a + 1500 * 86400, end)))
        out.update((_day(t), round(c, 3)) for t, c in rows)
    return out


def fetch_prices():
    """سعر الذهب (عقود COMEX الأقرب GC=F): إغلاقات يومية منذ 2008، وأسعار الساعة لآخر 5 أيام."""
    stored = load(PRICES_OUT, {})
    d = dict(stored.get("daily") or [])
    meta, bars = yahoo_chart("GC=F", "1mo", "1d", ohlc=True)
    recent = [(t, c) for t, _, _, _, c in bars]
    # أعلى وأدنى كل جلسة (لخريطة المستويات في فلتر الاتجاه): آخر 60 جلسة
    ohlc = {r[0]: r for r in stored.get("ohlc") or []}
    ohlc.update((_day(t), [_day(t), round(o, 2), round(h, 2), round(lo, 2), round(c, 2)]) for t, o, h, lo, c in bars)
    # range=max يعيد نقطة شهرية واحدة لا إغلاقات يومية، فنعيد بناء السجل مرة واحدة بفترات صريحة
    if sum(1 for k in d if k >= START_DAY_PRICES) < 3000:
        full = {}
        try:
            full = {k: round(v, 2) for k, v in yahoo_daily("GC=F", dt.date(2008, 1, 1)).items()}
        except Exception as e:
            log("daily price backfill failed:", str(e)[:200])
        if len(full) >= 3000:
            d = full
            log(f"daily price history rebuilt: {len(full)} closes")
        elif full:
            log(f"daily price backfill returned only {len(full)} points; keeping the stored history")
    for t, c in recent:
        d[_day(t)] = round(c, 2)
    d = {k: v for k, v in d.items() if k >= START_DAY_PRICES}
    try:
        _, hourly = yahoo_chart("GC=F", "5d", "60m")
        h = [[dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(microsecond=0).isoformat(), round(c, 2)] for t, c in hourly]
    except Exception as e:
        log("hourly prices failed:", e)
        h = (load(PRICES_OUT, {}).get("hourly") or [])
    price = meta.get("regularMarketPrice") or (h[-1][1] if h else None)
    ptime = meta.get("regularMarketTime")
    last = {"price": round(float(price), 2) if price else None,
            "time": dt.datetime.fromtimestamp(ptime, dt.timezone.utc).isoformat() if ptime else (h[-1][0] if h else None),
            # الإغلاق السابق = إغلاق الجلسة التي قبل الجلسة الأخيرة (chartPreviousClose يخص بداية النطاق، لا أمس)
            "prev_close": round(recent[-2][1], 2) if len(recent) > 1 else None}
    out = {"symbol": "GC=F", "updated_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           "last": last, "daily": sorted([k, v] for k, v in d.items()), "hourly": h[-150:],
           "ohlc": [ohlc[k] for k in sorted(ohlc)][-60:]}
    problems = series_issues("gold price", out["daily"], 300, 20000, jump=0.08, max_gap=5, tail=60)
    if last["price"] and last["prev_close"] and abs(last["price"] / last["prev_close"] - 1) > 0.08:
        problems.append(f"gold price: {(last['price'] / last['prev_close'] - 1) * 100:+.1f}% vs previous close")
    if problems:
        raise RuntimeError("data check: " + "; ".join(problems))
    save(PRICES_OUT, out)
    log(f"price ok: ${last['price']} at {last['time']} ({len(d)} daily closes, {len(h)} hourly)")
    return {}


# ---------------------------------------------------------------- macro drivers: real yield, nominal yield, breakeven, dollar
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}&cosd={}"
MACRO_FRED = {"real10": "DFII10", "nom10": "DGS10", "be10": "T10YIE", "vix": "VIXCLS",
              "fedu": "DFEDTARU", "effr": "DFF", "y2": "DGS2", "cpi": "CPIAUCSL", "core": "CPILFESL"}
MACRO_LONG = ("real10", "vix", "fedu", "effr", "y2")       # سجل منذ 2008 للاختبار التاريخي والرسوم الطويلة
MACRO_MONTHLY = ("cpi", "core")                           # CPI شهري: نجلبه منذ 2007 لنحسب التغير السنوي منذ 2008
MACRO_SHOW = "2018-01-01"                       # ما يُعرض في الصفحة؛ الاختبار التاريخي يستخدم السجل منذ 2008


def fred_series(sid, start=MACRO_SHOW):
    content = browser_get(FRED_CSV.format(sid, start), headers={"Accept": "text/csv"}).content.decode("utf-8", "replace")
    rows = []
    for line in content.splitlines()[1:]:
        day, _, v = line.partition(",")
        try:
            rows.append([day.strip(), round(float(v), 3)])
        except ValueError:
            pass                                    # "." = يوم عطلة بلا قيمة
    if len(rows) < 200:
        raise RuntimeError(f"FRED {sid}: only {len(rows)} rows")
    return rows


def fetch_macro():
    """محرّكات الذهب الكبرى: العائد الحقيقي 10 سنوات (TIPS)، والاسمي، والتضخم المتوقع، ومؤشر VIX (FRED)،
    ومؤشر الدولار DXY وعقود Nasdaq 100 (NQ) من Yahoo. ومنها مراقب البيع القسري."""
    full, errors = {}, []
    for key, sid in MACRO_FRED.items():
        try:
            full[key] = fred_series(sid, "2007-01-01" if key in MACRO_MONTHLY else "2008-01-01" if key in MACRO_LONG else MACRO_SHOW)
        except Exception as e:
            errors.append(f"{sid}: {str(e)[:90]}")
    for key, sym, start in (("dxy", "DX-Y.NYB", dt.date(2018, 1, 1)), ("nq", "NQ=F", dt.date(2008, 1, 1))):
        try:
            full[key] = sorted([k, round(v, 3)] for k, v in yahoo_daily(sym, start).items())
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:90]}")
    if "real10" not in full:
        raise RuntimeError("macro: " + " | ".join(errors))
    out = {k: [r for r in v if r[0] >= MACRO_SHOW] for k, v in full.items() if k not in MACRO_MONTHLY + ("fedu", "effr", "y2")}
    try:
        out["fed"] = fed_inflation(full)
    except Exception as e:
        log("fed/CPI block FAILED:", repr(e)[:200])
    if errors:
        log("macro partial:", " | ".join(errors))
        prev = load(OUT, {}).get("macro") or {}
        out = {**{k: v for k, v in prev.items() if k not in out}, **out}
    res = {"macro": out}
    try:
        res["liquidation"] = liquidation_monitor(full)
    except Exception as e:
        log("liquidation monitor FAILED:", repr(e)[:200])
    return res


def _monthly(rows):
    """آخر قيمة لكل شهر من سلسلة يومية."""
    m = {}
    for d, v in rows:
        m[d[:7]] = v
    return m


def fed_inflation(m):
    """الفيدرالي والتضخم: سعر الفائدة (الحد الأعلى DFEDTARU والفعلي DFF)، وعائد السنتين ناقص الفعلي كإشارة مبكرة
    لما يسعّره السوق، وCPI الكلي والأساسي سنويًا وآخر 3 أشهر سنويًا (تسارع أو تباطؤ)، والفائدة الحقيقية للفيدرالي."""
    cpi, core = m.get("cpi") or [], m.get("core") or []
    if len(cpi) < 30 or not m.get("effr"):
        raise RuntimeError("not enough CPI / Fed data")
    def yoy(rows):
        v = {r[0][:7]: r[1] for r in rows}
        out = []
        for d, x in rows:
            y0 = f"{int(d[:4]) - 1}{d[4:7]}"
            if y0 in v:
                out.append([d[:7], round((x / v[y0] - 1) * 100, 2)])
        return out
    def ann3(rows):
        return round(((rows[-1][1] / rows[-4][1]) ** 4 - 1) * 100, 2) if len(rows) > 4 else None
    cy, ky = yoy(cpi), yoy(core)
    effr = _monthly(m["effr"]); fedu = _monthly(m.get("fedu") or []); y2 = _monthly(m.get("y2") or [])
    months = sorted(effr)
    hist = []
    for mo in months:
        if mo < "2008-01":
            continue
        c = next((x[1] for x in cy if x[0] == mo), None)
        hist.append([mo, fedu.get(mo), effr[mo], y2.get(mo), c, round(effr[mo] - c, 2) if c is not None else None])
    last_effr, last_y2 = m["effr"][-1], (m.get("y2") or [[None, None]])[-1]
    spread = round(last_y2[1] - last_effr[1], 2) if last_y2[1] is not None else None
    fedu_rows = m.get("fedu") or []
    change = next(({"date": fedu_rows[i][0], "from": fedu_rows[i - 1][1], "to": fedu_rows[i][1]}
                   for i in range(len(fedu_rows) - 1, 0, -1) if fedu_rows[i][1] != fedu_rows[i - 1][1]), None)
    return {"fed_upper": fedu_rows[-1] if fedu_rows else None, "effr": last_effr, "y2": last_y2, "y2_minus_effr": spread,
            "last_fed_change": change,
            "cpi": {"month": cy[-1][0], "yoy": cy[-1][1], "mom": round((cpi[-1][1] / cpi[-2][1] - 1) * 100, 2), "ann3m": ann3(cpi),
                    "yoy_prev": cy[-2][1]},
            "core": {"month": ky[-1][0], "yoy": ky[-1][1], "mom": round((core[-1][1] / core[-2][1] - 1) * 100, 2), "ann3m": ann3(core),
                     "yoy_prev": ky[-2][1]},
            "real_fed": round(last_effr[1] - cy[-1][1], 2),
            "history": hist}            # [شهر، الحد الأعلى، الفعلي، عائد السنتين، CPI سنوي، فائدة حقيقية]


# ---------------------------------------------------------------- trend & base rates (for the AI report's calibration)
def trend_and_base_rates():
    """اتجاه الذهب: البعد عن متوسطات 20 و50 و200 جلسة، والبعد عن أعلى سعر في سنة، وأيام الهبوط المتتالية، والتقلب الفعلي.
    والنسب التاريخية منذ 2008 لنتيجة 10 جلسات (صعود/عرضي/هبوط ±2٪) في كل الأيام وفي الأيام المشابهة لليوم."""
    rows = [r for r in (load(PRICES_OUT, {}).get("daily") or []) if r[0] >= "2008-01-01"]
    if len(rows) < 400:
        return None
    px = [r[1] for r in rows]
    ma = lambda n, i: sum(px[i - n + 1:i + 1]) / n
    lr = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    vol = lambda i, n=20: statistics.pstdev(lr[i - n:i]) * math.sqrt(252) * 100
    i = len(px) - 1
    streak = 0
    while streak < i and px[i - streak] < px[i - streak - 1]:
        streak += 1
    hi = max(px[-252:])
    now = {"date": rows[-1][0], "price": px[-1], "vs_ma20_pct": round((px[i] / ma(20, i) - 1) * 100, 2),
           "vs_ma50_pct": round((px[i] / ma(50, i) - 1) * 100, 2), "vs_ma200_pct": round((px[i] / ma(200, i) - 1) * 100, 2),
           "from_52w_high_pct": round((px[i] / hi - 1) * 100, 2), "down_days_in_a_row": streak,
           "ret_1w_pct": round((px[i] / px[i - 5] - 1) * 100, 2), "ret_1m_pct": round((px[i] / px[i - 21] - 1) * 100, 2),
           "ret_3m_pct": round((px[i] / px[i - 63] - 1) * 100, 2), "realized_vol_20d_pct": round(vol(i), 1)}
    wk = lambda j: px[j] / px[j - 5] - 1
    bucket = lambda r: "≤-4%" if r <= -0.04 else "-4..-2%" if r <= -0.02 else "-2..+2%" if r < 0.02 else "+2..+4%" if r < 0.04 else "≥+4%"
    groups = {"all_days": lambda j: True,
              f"week_return_{bucket(wk(i))}": lambda j: bucket(wk(j)) == bucket(wk(i)),
              "same_side_of_ma200": lambda j: (px[j] > ma(200, j)) == (px[i] > ma(200, i)),
              "same_week_bucket_and_ma200_side": lambda j: bucket(wk(j)) == bucket(wk(i)) and (px[j] > ma(200, j)) == (px[i] > ma(200, i))}
    base = {}
    for name, f in groups.items():
        c = {"up": 0, "flat": 0, "down": 0}
        for j in range(200, len(px) - 10):
            if f(j):
                r = px[j + 10] / px[j] - 1
                c["up" if r > 0.02 else "down" if r < -0.02 else "flat"] += 1
        n = sum(c.values())
        if n:
            base[name] = {**{k: round(v / n * 100) for k, v in c.items()}, "n": n}
    return {"now": now, "base_rates_10_sessions": base,
            "note": "outcome after 10 sessions (≈14 calendar days): up > +2%, down < −2%, flat otherwise; COMEX GC since 2008"}


# ---------------------------------------------------------------- forced-selling (margin liquidation) monitor
LIQ = {"gold3d": -2.5, "nq3d": -3.0, "vix": 25.0, "vix3d": 25.0, "real3d": 0.05}


def liquidation_monitor(m):
    """هل هبوط الذهب يشبه بيعًا قسريًا لتغطية Margin في أصول أخرى؟ أربعة شروط في آخر 3 جلسات:
    الذهب −2.5٪ أو أكثر، و NQ −3٪ أو أكثر، و VIX فوق 25 أو ارتفع 25٪، والعائد الحقيقي لم يرتفع أكثر من 5bp
    (أي أن الهبوط لا يفسّره المحرّك المعتاد). ثم نقيس ما فعله الذهب بعد كل حالة مشابهة منذ 2008."""
    gold = [r for r in (load(PRICES_OUT, {}).get("daily") or []) if r[0] >= "2008-01-01"]
    nq, vix, real = dict(m.get("nq") or []), m.get("vix") or [], m.get("real10") or []
    if len(gold) < 300 or len(nq) < 300 or len(vix) < 300:
        raise RuntimeError("not enough history")
    vd, vv = [r[0] for r in vix], [r[1] for r in vix]
    rd, rv = [r[0] for r in real], [r[1] for r in real]
    at = lambda ds, vs, d: vs[bisect.bisect_right(ds, d) - 1] if bisect.bisect_right(ds, d) else None
    days = [r[0] for r in gold if r[0] in nq]
    g = dict(gold)
    def state(i):
        d, d0 = days[i], days[i - 3]
        v, v0, r1, r0 = at(vd, vv, d), at(vd, vv, d0), at(rd, rv, d), at(rd, rv, d0)
        x = {"date": d, "gold3d": round((g[d] / g[d0] - 1) * 100, 2), "nq3d": round((nq[d] / nq[d0] - 1) * 100, 2),
             "vix": v, "vix3d": round((v / v0 - 1) * 100, 1) if v and v0 else None,
             "real3d": round(r1 - r0, 3) if r1 is not None and r0 is not None else None}
        x["met"] = {"gold": x["gold3d"] <= LIQ["gold3d"], "nq": x["nq3d"] <= LIQ["nq3d"],
                    "vix": bool(v and (v >= LIQ["vix"] or (x["vix3d"] or 0) >= LIQ["vix3d"])),
                    "real": x["real3d"] is not None and x["real3d"] <= LIQ["real3d"]}
        return x
    episodes, last_sig = [], -99
    for i in range(3, len(days)):
        x = state(i)
        if all(x["met"].values()):
            if i - last_sig > 10:                          # حالة جديدة إن مرّ أكثر من 10 جلسات على السابقة
                fwd = {n: round((g[days[i + n]] / g[x["date"]] - 1) * 100, 2) if i + n < len(days) else None for n in (5, 10, 20)}
                pre = g[days[i - 3]]
                rec = next((k for k in range(1, 21) if i + k < len(days) and g[days[i + k]] >= pre), None)
                episodes.append({**{k: x[k] for k in ("date", "gold3d", "nq3d", "vix", "real3d")},
                                 "fwd5": fwd[5], "fwd10": fwd[10], "fwd20": fwd[20], "recovered_in": rec})
            last_sig = i
    done = [e for e in episodes if e["fwd20"] is not None]
    summ = {"n": len(episodes)}
    if done:
        f10, f20 = sorted(e["fwd10"] for e in done), sorted(e["fwd20"] for e in done)
        summ.update(up10=round(sum(v > 0 for v in f10) / len(f10) * 100), up20=round(sum(v > 0 for v in f20) / len(f20) * 100),
                    med10=f10[len(f10) // 2], med20=f20[len(f20) // 2],
                    recovered20=round(sum(e["recovered_in"] is not None for e in done) / len(done) * 100), scored=len(done))
    today = state(len(days) - 1)
    today["score"] = sum(today["met"].values())
    return {"today": today, "episodes": episodes[-25:], "summary": summ, "params": LIQ, "since": days[0]}


# ---------------------------------------------------------------- direction filter: tested rules only
# اختبار تاريخي (2008–2026، 45 عاملًا × 4 آفاق): لا عامل يتنبأ باتجاه الجلسة التالية بدقة تُذكر، إلا قاعدة واحدة ثابتة
# عبر الحقب: يوم هابط والسعر فوق MA200 → الجلسة التالية صاعدة ~57٪. أما حجم الحركة فقابل للتنبؤ (تقلّب 20 جلسة).
# لذلك يعطي الفلتر: النظام، والميزة الإحصائية إن وُجدت، ونطاقات σ، وخريطة المستويات، ومستوى الإبطال، ويُقيَّم يوميًا.
FILTER_LOG = os.path.join("data", "filter_log.json")
NEW_YORK = ZoneInfo("America/New_York")
FILTER_BANDS = (1.0, 1.5, 2.0)
FILTER_ERAS = (("2008-01-01", "2019-12-31"), ("2020-01-01", "2099-12-31"))
FILTER_SWING = (5, 10)                      # نطاقات السوينغ: أسبوع وأسبوعان
LV_GROUPS = {"gex": ("zero_gamma", "call_wall", "put_wall", "put_2", "put_3", "call_2", "call_3"),
             "sigma": ("+1s", "-1s", "+1.5s", "-1.5s", "+2s", "-2s"), "prior": ("pdh", "pdl", "pwh", "pwl"),
             "ma": ("ma50", "ma200")}
SCORECARD = os.path.join("data", "scorecard.json")


def _session_done(day, now=None):
    """جلسة GC بتاريخ معيّن تنتهي 17:00 بتوقيت نيويورك؛ قبلها يكون آخر سطر يومي سعرًا جاريًا لا إغلاقًا."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now >= dt.datetime.combine(dt.date.fromisoformat(day), dt.time(17, 0), NEW_YORK)


def _pct(xs):
    return round(sum(xs) / len(xs) * 100, 1) if xs else None


def direction_filter(d, now=None):
    P = load(PRICES_OUT, {})
    rows = [r for r in P.get("daily") or [] if r[1]]
    if rows and not _session_done(rows[-1][0], now):
        rows = rows[:-1]
    if len(rows) < 600:
        raise RuntimeError("not enough price history")
    days, px = [r[0] for r in rows], [r[1] for r in rows]
    n = len(px)
    lr = [0.0] + [math.log(px[i] / px[i - 1]) for i in range(1, n)]
    cs = [0.0]
    for v in px:
        cs.append(cs[-1] + v)
    ma = lambda i, w: (cs[i + 1] - cs[i + 1 - w]) / w if i + 1 >= w else None
    vol = [None] * n
    for i in range(20, n):
        vol[i] = statistics.stdev(lr[i - 19:i + 1])
    feat = lambda i: (lr[i] < 0, px[i] > ma(i, 200))
    # إحصاءات تاريخية تُعاد حسابها كل يوم (لا أرقام مكتوبة باليد)
    buckets, cover, base = {}, {k: [] for k in FILTER_BANDS}, []
    eras = [{"from": a[:4], "n": 0, "up1": [], "up10": []} for a, _ in FILTER_ERAS]
    reg10, swing_cov = {}, {h: [] for h in FILTER_SWING}
    regime_at = lambda i: ("up" if px[i] > ma(i, 200) and px[i] > px[i - 120] else
                           "down" if px[i] < ma(i, 200) and px[i] < px[i - 120] else "mixed")
    for i in range(250, n - 1):
        dn, above = feat(i)
        if i + 10 < n:
            reg10.setdefault(regime_at(i), []).append(px[i + 10] > px[i])
        for h in FILTER_SWING:
            if i + h < n:
                swing_cov[h].append(abs(math.log(px[i + h] / px[i])) <= vol[i] * math.sqrt(h))
        nx = lr[i + 1]
        b = buckets.setdefault(("down" if dn else "up") + "_" + ("above" if above else "below"), {"up1": [], "up5": []})
        b["up1"].append(nx > 0)
        if i + 5 < n:
            b["up5"].append(px[i + 5] > px[i])
        base.append(nx > 0)
        for k in FILTER_BANDS:
            cover[k].append(abs(nx) <= k * vol[i])
        if dn and above:
            for e, (a, z) in zip(eras, FILTER_ERAS):
                if a <= days[i] <= z:
                    e["up1"].append(nx > 0)
                    if i + 10 < n:
                        e["up10"].append(px[i + 10] > px[i])
    stats = {k: {"n": len(v["up1"]), "up1": _pct(v["up1"]), "up5": _pct(v["up5"])} for k, v in buckets.items()}
    eras = [{"from": e["from"], "n": len(e["up1"]), "up1": _pct(e["up1"]), "up10": _pct(e["up10"])} for e in eras]
    i = n - 1
    c, m200, m50, sig = px[i], ma(i, 200), ma(i, 50), vol[i]
    mom120 = math.log(c / px[i - 120]) * 100
    dn, above = feat(i)
    regime = "up" if above and mom120 > 0 else "down" if not above and mom120 < 0 else "mixed"
    signal = "long_edge" if dn and above else "none"
    su = c * sig
    bands = [{"k": k, "lo": round(c - k * su, 1), "hi": round(c + k * su, 1), "cover": _pct(cover[k])} for k in FILTER_BANDS]
    levels = [{"id": "close", "v": round(c, 1)}, {"id": "ma200", "v": round(m200, 1)}, {"id": "ma50", "v": round(m50, 1)}]
    for b in bands:
        levels += [{"id": f"+{b['k']:g}s", "v": b["hi"]}, {"id": f"-{b['k']:g}s", "v": b["lo"]}]
    bars = {r[0]: r for r in P.get("ohlc") or []}
    if days[i] in bars:
        levels += [{"id": "pdh", "v": bars[days[i]][2]}, {"id": "pdl", "v": bars[days[i]][3]}]
    # الأسبوع السابق = آخر أسبوع اكتمل (إن كان آخر إغلاق جمعة فهو أسبوعه)
    di = dt.date.fromisoformat(days[i])
    wk_end = di if di.weekday() >= 4 else di - dt.timedelta(days=di.weekday() + 3)
    wk = [r for k, r in bars.items() if wk_end - dt.timedelta(days=6) <= dt.date.fromisoformat(k) <= wk_end]
    if len(wk) >= 3:
        levels += [{"id": "pwh", "v": max(r[2] for r in wk)}, {"id": "pwl", "v": min(r[3] for r in wk)}]
    g, gex = d.get("gex") or {}, None
    if g.get("zero_all") and g.get("gc_ratio") and g.get("spot"):
        r = g["gc_ratio"]
        gex = {"regime": "positive" if g["spot"] >= g["zero_all"] else "negative", "asof": g.get("asof"),
               "zero": round(g["zero_all"] * r, 1), "call_wall": round((g.get("call_wall") or 0) * r, 1) or None,
               "put_wall": round((g.get("put_wall") or 0) * r, 1) or None}
        levels += [{"id": "zero_gamma", "v": gex["zero"]}]
        levels += [{"id": k, "v": gex[k], "k": g.get(k)} for k in ("call_wall", "put_wall") if gex[k]]
        # ثاني وثالث أكبر تركّز للـ Puts وللـ Calls (الجدار وحده يُخفي Strikes قريبة منه في الحجم)
        walls = {g.get("put_wall"), g.get("call_wall")}
        for side, col in (("put", 2), ("call", 1)):
            top = sorted((x for x in g.get("strikes") or [] if x[0] not in walls and (x[col] < 0 if side == "put" else x[col] > 0)),
                         key=lambda x: -abs(x[col]))[:2]
            levels += [{"id": f"{side}_{j}", "v": round(x[0] * r, 1), "k": x[0], "m": x[col]} for j, x in enumerate(top, 2)]
    if signal == "long_edge":
        inval = {"kind": "long_void_below", "v": round(max(m200, c - 2 * su), 1)}
    elif not above:
        inval = {"kind": "regime_up_above", "v": round(m200, 1)}
    else:
        inval = {"kind": "wait_down_close", "v": round(m200, 1)}
    cur = {"date": days[i], "close": round(c, 2), "signal": signal, "regime": regime, "above200": above,
           "sigma": round(sig * 100, 3), "z": round(lr[i] / vol[i - 1], 2) if vol[i - 1] else None,
           "gex": gex and gex["regime"], "zero": gex and gex["zero"],
           "levels": [[x["id"], x["v"]] for x in levels if x["id"] != "close"]}
    swing = [{"days": h, "usd": round(su * math.sqrt(h), 1), "lo": round(c - su * math.sqrt(h), 1),
              "hi": round(c + su * math.sqrt(h), 1), "cover": _pct(swing_cov[h])} for h in FILTER_SWING]
    return {"asof": days[i], "close": round(c, 2), "day_ret": round(lr[i] * 100, 2), "z": cur["z"],
            "ma200": round(m200, 1), "ma50": round(m50, 1), "mom120": round(mom120, 1), "above200": above,
            "regime": regime, "signal": signal, "bucket": ("down" if dn else "up") + "_" + ("above" if above else "below"),
            "sigma_pct": round(sig * 100, 2), "sigma_usd": round(su, 1), "bands": bands,
            "levels": sorted(levels, key=lambda x: -x["v"]), "gex": gex, "invalidation": inval,
            "swing": swing, "regime_up10": {k: {"n": len(v), "up10": _pct(v)} for k, v in reg10.items()},
            "stats": {"buckets": stats, "base_up1": _pct(base), "edge_eras": eras, "since": days[250]},
            "track": score_filter(cur, days, px, bars)}


TV_KEYS = {"zero_gamma": "ZG", "call_wall": "CW", "put_wall": "PW", "put_2": "P2", "put_3": "P3", "call_2": "C2",
           "call_3": "C3", "+1s": "U1", "-1s": "D1", "+1.5s": "U15", "-1.5s": "D15", "+2s": "U2", "-2s": "D2",
           "pdh": "PDH", "pdl": "PDL", "pwh": "PWH", "pwl": "PWL", "ma50": "MA50", "ma200": "MA200"}


def _focus_zones(raw, df):
    """مناطق Fable: يختار معرّفات من مستويات الفلتر فقط، والكود يحسب السعر، فلا يكتب النموذج أسعارًا من عنده."""
    if not isinstance(raw, list) or not df or not df.get("levels"):
        return None
    lv = {x["id"]: x["v"] for x in df["levels"] if x.get("id") != "close"}
    span = 1.5 * (df.get("sigma_usd") or 50)
    zones = []
    for z in raw[:4]:
        if not isinstance(z, dict) or z.get("role") not in ("support", "resistance", "pivot"):
            continue
        ids = [i for i in (z.get("ids") or [])[:3] if i in lv]
        if not ids:
            continue
        lo, hi = min(lv[i] for i in ids), max(lv[i] for i in ids)
        if hi - lo > span:
            continue
        zones.append({"ids": ids, "lo": round(lo, 1), "hi": round(hi, 1), "role": z["role"], "why": str(z.get("why") or "")[:200]})
    return {"asof": df.get("asof"), "zones": zones} if zones else None


def tv_code(df, ai):
    """سطر واحد يُلصق في مؤشر TradingView: الإصدار|التاريخ|النظام|الميزة|Gamma|المستويات|مناطق Fable.
    مناطق Fable تُضاف فقط إن بُنيت على مستويات الجلسة نفسها."""
    lv = ";".join(f"{TV_KEYS[x['id']]}={x['v']:.1f}" for x in df.get("levels") or [] if x.get("id") in TV_KEYS)
    gx = {"positive": "pos", "negative": "neg"}.get((df.get("gex") or {}).get("regime"), "na")
    focus = ((ai or {}).get("latest") or {}).get("focus") or {}
    zones = focus.get("zones") if focus.get("asof") == df.get("asof") else []
    zs = ",".join(f"{z['role'][0].upper()}:{z['lo']:.1f}-{z['hi']:.1f}" for z in zones or [])
    edge = "long" if df.get("signal") == "long_edge" else "none"
    return f"GFL1|{df.get('asof')}|{df.get('regime')}|{edge}|{gx}|{lv}|{zs}", (zones or [])


def score_filter(cur, days, px, bars=None):
    """يسجّل قراءة كل جلسة مكتملة ويقيّمها بعد الجلسة التالية: هل صدقت الميزة؟ وهل بقي الإغلاق داخل نطاقات σ؟"""
    log_ = load(FILTER_LOG, [])
    by = {e["date"]: e for e in log_}
    by.setdefault(cur["date"], cur)
    by[cur["date"]].setdefault("levels", cur["levels"])     # القراءة تُجمَّد عند أول تسجيل؛ نكمل المستويات فقط إن نقصت
    pos = {k: j for j, k in enumerate(days)}
    for e in by.values():
        j = pos.get(e["date"])
        if j is None:
            continue
        if "r1" not in e and j + 1 < len(px):
            e["r1"] = round(math.log(px[j + 1] / px[j]) * 100, 3)
        if "up5" not in e and j + 5 < len(px):
            e["up5"] = px[j + 5] > px[j]
        # المستويات: هل لمسها سعر الجلسة التالية؟ وإن لمسها، هل صمدت (أغلق السعر على جهته الأصلية) أم كُسرت؟
        if "lv" not in e and e.get("levels") and j + 1 < len(days) and (bars or {}).get(days[j + 1]):
            b = bars[days[j + 1]]
            hi, lo, c1 = b[2], b[3], px[j + 1]
            res = []
            for lid, v in e["levels"]:
                up = v > e["close"]
                t = hi >= v if up else lo <= v
                res.append([lid, int(t), (int(c1 < v) if up else int(c1 > v)) if t else None])
            e["lv"] = res
    log_ = [by[k] for k in sorted(by)][-500:]
    save(FILTER_LOG, log_)
    done = [e for e in log_ if "r1" in e]
    edge = [e for e in done if e["signal"] == "long_edge"]
    none = [e for e in done if e["signal"] != "long_edge"]
    out = {"since": log_[0]["date"], "scored": len(done),
           "edge_n": len(edge), "edge_up1": _pct([e["r1"] > 0 for e in edge]),
           "edge_up5": _pct([e["up5"] for e in edge if "up5" in e]),
           "none_up1": _pct([e["r1"] > 0 for e in none]),
           "cover": {f"{k:g}": _pct([abs(e["r1"]) <= k * e["sigma"] for e in done]) for k in FILTER_BANDS}}
    gx = {}
    for e in done:
        if e.get("gex"):
            gx.setdefault(e["gex"], []).append(abs(e["r1"]) / e["sigma"])
    out["gex_move_sigma"] = {k: {"n": len(v), "avg": round(sum(v) / len(v), 2)} for k, v in gx.items()}
    lv = {g_: {"touched": 0, "held": 0} for g_ in LV_GROUPS}
    by_id = {}
    for e in log_:
        for lid, t, h in e.get("lv") or []:
            grp = next((k for k, ids in LV_GROUPS.items() if lid in ids), None)
            x = by_id.setdefault(lid, {"touched": 0, "held": 0, "days": 0})
            x["days"] += 1
            if t:
                x["touched"] += 1
                x["held"] += h
                if grp:
                    lv[grp]["touched"] += 1
                    lv[grp]["held"] += h
    out["levels"] = {k: {"touched": v["touched"], "held": round(v["held"] / v["touched"] * 100, 1) if v["touched"] else None}
                     for k, v in lv.items()}
    out["levels_by_id"] = by_id
    out["rows"] = [{k: e.get(k) for k in ("date", "close", "signal", "regime", "sigma", "r1", "gex")} for e in log_[-15:]]
    return out


# ---------------------------------------------------------------- weekly scorecard (rules fixed in advance)
# قواعد الحكم مكتوبة مسبقًا كي لا تُفسَّر الأرقام لاحقًا لصالح المنصة. يقرأ هذا الملف التقرير الأسبوعي على البريد.
SC_RULES = {
    "bands": {"min_n": 60, "tol_pts": 5},          # كل نطاق σ في حدود ±5 نقاط من نسبته التاريخية بعد 60 جلسة
    "edge": {"min_n": 30, "pass_pct": 55},         # ميزة Long تصيب 55٪ أو أكثر بعد 30 إشارة
    "fable": {"prelim_windows": 13, "min_windows": 26},   # مهارة Brier موجبة مقابل المعدل التاريخي، بنوافذ 14 يومًا مستقلة
    "fable_direction": {"prelim_windows": 13, "min_windows": 26},   # في النوافذ التي تحرك فيها السعر أكثر من 2٪ فقط
    "levels": {"min_touch": 20, "margin_pts": 10},  # مستويات GEX تصمد أكثر من المستويات العادية بـ 10 نقاط
    "gex_regime": {"min_n": 20, "margin_sigma": 0.15},   # الحركة في Gamma سالبة أكبر منها في الموجبة
}


def _windows(dates, days=None):
    """عدد النوافذ المستقلة (لا تتداخل) بين تواريخ التوقعات."""
    days, n, last = days or OUTLOOK_DAYS, 0, None
    for d in sorted(dt.date.fromisoformat(x) for x in dates):
        if last is None or (d - last).days >= days:
            n, last = n + 1, d
    return n


def scorecard(data, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    df = data.get("direction_filter") or {}
    tr = df.get("track") or {}
    R, out = SC_RULES, {"generated_utc": now.replace(microsecond=0).isoformat(), "rules": SC_RULES}
    # 1) نطاقات σ
    n = tr.get("scored") or 0
    exp = {f"{b['k']:g}": b["cover"] for b in df.get("bands") or []}
    got = tr.get("cover") or {}
    v = "not_yet" if n < R["bands"]["min_n"] else (
        "pass" if all(got.get(k) is not None and abs(got[k] - exp[k]) <= R["bands"]["tol_pts"] for k in exp) else "fail")
    out["bands"] = {"verdict": v, "n": n, "need": R["bands"]["min_n"], "actual": got, "expected": exp}
    # 2) ميزة Long
    n = tr.get("edge_n") or 0
    v = "not_yet" if n < R["edge"]["min_n"] else ("pass" if (tr.get("edge_up1") or 0) >= R["edge"]["pass_pct"] else "fail")
    out["edge"] = {"verdict": v, "n": n, "need": R["edge"]["min_n"], "hit_next": tr.get("edge_up1"), "hit_5": tr.get("edge_up5"),
                   "no_edge_days_up": tr.get("none_up1"), "signal_now": df.get("signal"), "regime_now": df.get("regime")}
    # 3) انحياز Fable
    ot = data.get("outlook_track") or {}
    s = ot.get("summary") or {}
    scored = [e for e in ot.get("rows") or [] if e.get("result")]
    w = _windows([e["base"]["date"] for e in scored if e.get("base")])
    skill = s.get("skill")
    beat_guess = s.get("brier") is not None and s["brier"] < s.get("brier_naive", 0.667)
    if w < R["fable"]["prelim_windows"]:
        v = "not_yet"
    elif w < R["fable"]["min_windows"]:
        v = "preliminary_positive" if (skill or 0) > 0 else "preliminary_negative"
    else:
        v = "pass" if (skill or 0) > 0 else "fail"
    week_ago = (now - dt.timedelta(days=7)).date().isoformat()
    out["fable"] = {"verdict": v, "scored": s.get("n", 0), "pending": s.get("pending", 0), "independent_windows": w,
                    "need": R["fable"]["min_windows"], "hit_pct": s.get("hit"), "brier": s.get("brier"),
                    "brier_guess": s.get("brier_naive", 0.667), "brier_base_rate": s.get("brier_base"), "skill_vs_base_pct": skill,
                    "beats_guessing": beat_guess,
                    "scored_this_week": [{"date": e["date"], "bias": e["bias"], "p": dict(zip(e["dirs"], e["p"])),
                                          "outcome": e["result"]["outcome"], "ret": e["result"]["ret"], "brier": e["result"]["brier"]}
                                         for e in scored if e["result"]["date"] >= week_ago]}
    # 3ب) اتجاه Fable وحده (النوافذ الحاسمة فقط): تحتاج نحو سنتين لـ 26 نافذة مستقلة
    dr = s.get("direction") or {}
    w = dr.get("windows", 0)
    good = (dr.get("skill") or 0) > 0 if dr.get("skill") is not None else ((dr.get("hit") or 0) > 50 and (dr.get("brier") or 1) < 0.25)
    if w < R["fable_direction"]["prelim_windows"]:
        v = "not_yet"
    elif w < R["fable_direction"]["min_windows"]:
        v = "preliminary_positive" if good else "preliminary_negative"
    else:
        v = "pass" if good else "fail"
    out["fable_direction"] = {"verdict": v, "decisive": dr.get("n", 0), "independent_windows": w,
                              "need": R["fable_direction"]["min_windows"], "lean_hit_pct": dr.get("hit"), "n_lean": dr.get("n_lean", 0),
                              "brier": dr.get("brier"), "brier_coin": 0.25, "brier_base_rate": dr.get("brier_base"),
                              "skill_vs_base_pct": dr.get("skill")}
    # 4) المستويات: GEX مقابل المستويات العادية (σ وأعلى/أدنى سابق)
    lv = tr.get("levels") or {}
    gx = lv.get("gex") or {}
    ctrl_t = sum((lv.get(k) or {}).get("touched", 0) for k in ("sigma", "prior"))
    ctrl_h = sum(((lv.get(k) or {}).get("held") or 0) * (lv.get(k) or {}).get("touched", 0) / 100 for k in ("sigma", "prior"))
    ctrl = round(ctrl_h / ctrl_t * 100, 1) if ctrl_t else None
    if (gx.get("touched") or 0) < R["levels"]["min_touch"] or ctrl_t < R["levels"]["min_touch"]:
        v = "not_yet"
    else:
        v = "pass" if gx["held"] - ctrl >= R["levels"]["margin_pts"] else "fail"
    out["levels"] = {"verdict": v, "need_touches": R["levels"]["min_touch"], "groups": lv, "control_held": ctrl,
                     "control_touched": ctrl_t, "by_level": tr.get("levels_by_id")}
    # 5) نظام GEX: هل الحركة في Gamma سالبة أكبر فعلًا؟
    gm = tr.get("gex_move_sigma") or {}
    neg, pos = gm.get("negative") or {}, gm.get("positive") or {}
    if (neg.get("n") or 0) < R["gex_regime"]["min_n"] or (pos.get("n") or 0) < R["gex_regime"]["min_n"]:
        v = "not_yet"
    else:
        v = "pass" if neg["avg"] - pos["avg"] >= R["gex_regime"]["margin_sigma"] else "fail"
    out["gex_regime"] = {"verdict": v, "need_each": R["gex_regime"]["min_n"], "negative": neg, "positive": pos}
    # 6) الأسبوع
    out["week"] = [r for r in tr.get("rows") or [] if r["date"] >= week_ago]
    out["now"] = {k: df.get(k) for k in ("asof", "close", "regime", "signal", "sigma_usd", "swing", "invalidation", "gex")}
    ai = (data.get("ai") or {}).get("history") or []
    if ai:
        last = ai[-1]
        out["now"]["fable_bias"] = {"date": last.get("date"), "outlook": last.get("outlook")}
    return out


# ---------------------------------------------------------------- GEX on GLD (Cboe delayed options, free)
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
OCC = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")


def _bs_gamma(S, K, T, iv, r=0.04):
    v = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / v
    return math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (S * v)


def _zero_cross(profile, S):
    """أقرب سعر إلى السعر الحالي ينقلب عنده مجموع GEX من سالب إلى موجب."""
    best = None
    for (x1, y1), (x2, y2) in zip(profile, profile[1:]):
        if y1 == 0 or (y1 < 0) != (y2 < 0):
            z = x1 if y1 == y2 else x1 + (x2 - x1) * (-y1) / (y2 - y1)
            if best is None or abs(z - S) < abs(best - S):
                best = z
    return round(best, 2) if best is not None else None


def gex_from_chain(opts, S, now):
    """GEX بالدولار لكل حركة 1٪: Gamma × OI × 100 × S² × 0.01، الـ Calls موجبة والـ Puts سالبة (العرف المعتاد:
    المتعاملون Long Calls و Short Puts). Zero Gamma من إعادة حساب Gamma بنموذج Black-Scholes عند أسعار افتراضية."""
    ivs = sorted(num(o.get("iv")) or 0 for o in opts)
    iv_scale = 100 if ivs and ivs[len(ivs) // 2] > 3 else 1         # إن جاءت التقلبات الضمنية بالنسبة المئوية لا كسرًا عشريًا
    rows = []
    for o in opts:
        m = OCC.match(str(o.get("option", "")))
        oi, iv = num(o.get("open_interest")) or 0, (num(o.get("iv")) or 0) / iv_scale
        if not m or oi <= 0 or iv <= 0.01:
            continue
        K = int(m.group(4)) / 1000
        if not 0.6 * S <= K <= 1.4 * S:
            continue
        exp = dt.datetime.strptime(m.group(2), "%y%m%d").replace(hour=20, tzinfo=dt.timezone.utc)
        T = (exp - now).total_seconds() / (365 * 86400)
        if T <= 0:
            continue
        rows.append((K, exp.date().isoformat(), 1 if m.group(3) == "C" else -1, oi, iv, max(T, 1 / (365 * 24))))
    if len(rows) < 100:
        raise RuntimeError(f"GEX: only {len(rows)} usable options")
    gex = lambda x, sel: sum(sg * _bs_gamma(x, K, T, iv) * oi * 100 * x * x * 0.01 for K, e, sg, oi, iv, T in sel)
    grid = [S * (0.85 + 0.0025 * i) for i in range(121)]
    near = min(r[1] for r in rows)
    near_rows = [r for r in rows if r[1] == near]
    prof_all = [(x, gex(x, rows)) for x in grid]
    prof_near = [(x, gex(x, near_rows)) for x in grid]
    by_k, walls = {}, {}
    struct = [r for r in rows if r[5] > 1.5 / 365] or rows    # الجدران هيكلية: نستبعد عقود اليوم الأخير التي تتركّز عند السعر الحالي
    for K, e, sg, oi, iv, T in rows:
        g = _bs_gamma(S, K, T, iv) * oi * 100 * S * S * 0.01
        by_k.setdefault(K, [0.0, 0.0])[0 if sg > 0 else 1] += g * sg
    for K, e, sg, oi, iv, T in struct:
        walls.setdefault(K, [0.0, 0.0])[0 if sg > 0 else 1] += _bs_gamma(S, K, T, iv) * oi * 100 * S * S * 0.01 * sg
    above = {K: v for K, v in walls.items() if S <= K <= 1.10 * S} or walls     # جدار قريب يمكن أن يُختبر خلال أسابيع،
    below = {K: v for K, v in walls.items() if 0.90 * S <= K <= S} or walls     # لا عقود LEAPS بعيدة بفائدة مفتوحة ضخمة
    call_wall = max(above, key=lambda K: above[K][0])            # أكبر Gamma للـ Calls عند السعر أو فوقه
    put_wall = min(below, key=lambda K: below[K][1])             # أكبر Gamma للـ Puts عند السعر أو تحته
    return {"spot": round(S, 2), "total_m": round(gex(S, rows) / 1e6, 1), "near_total_m": round(gex(S, near_rows) / 1e6, 1),
            "zero_all": _zero_cross(prof_all, S), "zero_near": _zero_cross(prof_near, S), "near_exp": near,
            "call_wall": call_wall, "put_wall": put_wall, "n_options": len(rows),
            "profile": [[round(x, 2), round(y / 1e6, 2)] for x, y in prof_all[::2]],
            "strikes": [[K, round(v[0] / 1e6, 2), round(v[1] / 1e6, 2)] for K, v in sorted(by_k.items()) if 0.88 * S <= K <= 1.12 * S]}


def fetch_gex():
    """مستويات Gamma لصندوق GLD من بيانات Cboe المؤجلة 15 دقيقة. نحفظ المستويات المحسوبة فقط، لا الأسعار الخام."""
    j = browser_get(CBOE_URL.format("GLD"), headers={"Accept": "application/json"}).json()
    d = j.get("data") or {}
    S = num(d.get("current_price")) or num(d.get("close"))
    if not S:
        raise RuntimeError("Cboe GLD: no current price")
    now = dt.datetime.now(dt.timezone.utc)
    g = gex_from_chain(d.get("options") or [], S, now)
    g["asof"] = now.replace(microsecond=0).isoformat()          # لحظة الجلب (UTC)؛ توقيت Cboe الخام بلا منطقة زمنية
    g["cboe_ts"] = str(j.get("timestamp") or "")
    gc = (load(PRICES_OUT, {}).get("last") or {}).get("price")
    g["gc_ratio"] = round(gc / S, 4) if gc else None                # مستوى GLD × النسبة ≈ مستوى عقد الذهب GC
    hist = [h for h in load(GEX_HIST, []) if h.get("d") != now.date().isoformat()]
    hist.append({"d": now.date().isoformat(), **{k: g[k] for k in ("spot", "total_m", "zero_all", "zero_near", "call_wall", "put_wall", "gc_ratio")}})
    save(GEX_HIST, hist[-120:])
    g["history"] = hist[-30:]
    log(f"GEX ok: GLD {S}, net {g['total_m']}M per 1%, zero {g['zero_all']} / near {g['zero_near']}, walls {g['put_wall']}-{g['call_wall']}")
    return {"gex": g}


# ---------------------------------------------------------------- World Gold Council: global ETF flows (monthly)
WGC_URL = "https://www.gold.org/goldhub/research/gold-etfs-holdings-and-flows/{}/{:02d}"
WGC_MODEL = "claude-haiku-5-5"
WGC_SYSTEM = """Extract numbers from a World Gold Council monthly "Gold ETF Flows" article. Return ONLY one JSON object:
{"data_month":"YYYY-MM","global_holdings_t":number|null,"global_demand_t":number|null,"global_flow_usd_bn":number|null,
 "aum_usd_bn":number|null,
 "regions":{"north_america":{"flow_usd_bn":number|null,"demand_t":number|null},"europe":{...},"asia":{...},"other":{...}},
 "headline_ar":"جملة واحدة بالعربية الفصحى"}
Use null when a number is not stated. Outflows are negative. data_month is the month the data covers (not the publication month).
US$ millions must be converted to billions (e.g. US$495mn = 0.495)."""


def fetch_wgc():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("needs the ANTHROPIC_API_KEY secret to read the monthly WGC article")
    hist = load(WGC_HIST, [])
    done = {h.get("article") for h in hist}
    today = dt.date.today().replace(day=1)
    errors, added = [], 0
    for back in (range(0, 13) if len(hist) < 6 else (0, 1, 2)):    # أول مرة: سنة كاملة من التقارير الشهرية
        m = add_months(today, -back)
        url = WGC_URL.format(m.year, m.month)
        if url in done:
            continue
        try:
            html = browser_get(url, allow_html=True).content.decode("utf-8", "replace")
        except Exception as e:
            errors.append(f"{m:%Y-%m}: {str(e)[:90]}")
            continue
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        i = text.find("Gold ETF Flows:")
        if i < 0:
            errors.append(f"{m:%Y-%m}: article not found")
            continue
        body = text[i:i + 14000]
        resp = _claude({"model": WGC_MODEL, "max_tokens": 900, "system": WGC_SYSTEM,
                        "messages": [{"role": "user", "content": body}]}, key)
        out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        j = json.loads(out[out.find("{"):out.rfind("}") + 1])
        if not re.fullmatch(r"\d{4}-\d{2}", str(j.get("data_month", ""))):
            errors.append(f"{m:%Y-%m}: no data_month")
            continue
        j["article"] = url
        j["seen_utc"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        hist = [h for h in hist if h.get("data_month") != j["data_month"]] + [j]
        added += 1
        log(f"WGC ok: data month {j['data_month']}, global holdings {j.get('global_holdings_t')} t")
    hist.sort(key=lambda h: h.get("data_month", ""))
    save(WGC_HIST, hist)
    if not hist:
        raise RuntimeError("WGC: " + (" | ".join(errors) or "nothing found")[:250])
    if not added:
        log("WGC: no new monthly article", ("(" + " | ".join(errors)[:150] + ")") if errors else "")
    return {"wgc_etf": hist[-36:]}


# ---------------------------------------------------------------- SEC 13F (institutions)
F13_BASES = [                                   # SEC غيّرت مسار الملفات الأحدث؛ نجرّب كل المسارات المعروفة
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/",
    "https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/",
    "https://dcm.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/",
    "https://dcm.sec.gov/files/structureddata/data/form-13f-data-sets/",
]
F13_START = dt.date(2024, 6, 1)
def sec_ua():
    """SEC تشترط User-Agent فيه اسم وبريد للتواصل. يُقرأ البريد من GitHub Secret اسمه SEC_CONTACT إن وُجد."""
    c = " ".join(os.environ.get("SEC_CONTACT", "").replace('"', " ").replace("'", " ").split())   # سطر واحد دون فراغات زائدة
    return f"GoldFlowsDashboard {c}" if "@" in c else "GoldFlowsDashboard abduswaidan-glitch@users.noreply.github.com"


def sec_hint(content):
    t = content[:4000].decode("utf-8", "replace")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for key in ("Undeclared Automated Tool", "Request Rate Threshold", "Access Denied", "Your Request"):
        i = t.find(key)
        if i >= 0:
            return t[i:i + 90]
    return t[:90]


def sec_download(url, path):
    """ينزّل ملف SEC إلى path. يعيد 200 أو 404، أو يرفع خطأ يشرح سبب الرفض."""
    ua, errors = sec_ua(), []
    try:
        with requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, stream=True, timeout=600) as r:
            if r.status_code == 404:
                return 404
            if r.status_code == 200:
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                return 200
            errors.append(f"requests HTTP {r.status_code} [{sec_hint(r.raw.read(4000, decode_content=True) or b'')}]")
    except Exception as e:
        errors.append(f"requests: {e}")
    try:
        creq = ensure("curl_cffi.requests", "curl_cffi")
        for imp in ("chrome", "safari"):
            r = creq.get(url, impersonate=imp, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=900)
            if r.status_code == 404:
                return 404
            if r.status_code == 200 and r.content[:2] == b"PK":
                with open(path, "wb") as f:
                    f.write(r.content)
                return 200
            errors.append(f"{imp} HTTP {r.status_code} [{sec_hint(r.content)}]")
    except Exception as e:
        errors.append(f"curl_cffi: {e}")
    raise RuntimeError(" | ".join(errors)[:300])
FUND_CUSIPS = {"78463V107": "GLD", "464285204": "IAU", "464285105": "IAU"}
FUND_NAMES = [
    ("IAUM", r"ISHARES GOLD (TR|TRUST)\W*MICRO"),
    ("GLDM", r"SPDR GOLD MINI|WORLD GOLD TR"),
    ("SGOL", r"ABRDN (PHYSICAL )?GOLD|ABRDN GOLD ETF|ABERDEEN STANDARD GOLD|ETFS GOLD TR"),
    ("AAAU", r"GOLDMAN SACHS PHYSICAL GOLD|PERTH MINT PHYSICAL GOLD"),
    ("BAR", r"GRANITESHARES GOLD"),
]
MAJORS = (r"GOLDMAN SACHS|JPMORGAN|J\.?P\.? ?MORGAN|MORGAN STANLEY|BANK OF AMERICA|BANK AMERICA|MERRILL|CITIGROUP|WELLS FARGO|"
          r"\bUBS\b|BARCLAYS|HSBC|DEUTSCHE BANK|BNP PARIBAS|ROYAL BANK OF CANADA|TORONTO.?DOMINION|BANK OF MONTREAL|SCOTIA|"
          r"BLACKROCK|VANGUARD|STATE STREET|\bFMR\b|FIDELITY|BRIDGEWATER|RENAISSANCE TECH|CITADEL|MILLENNIUM MANAGEMENT|"
          r"SUSQUEHANNA|JANE STREET|TWO SIGMA|D\.? ?E\.? SHAW|PAULSON|SOROS|NORTHERN TRUST|SCHWAB|NOMURA|MIZUHO|MITSUBISHI UFJ|"
          r"SUMITOMO|SOCIETE GENERALE|CREDIT AGRICOLE|JEFFERIES|NORGES BANK|INVESCO|AMERIPRISE|RAYMOND JAMES|LPL FINANCIAL")


def add_months(d, n):
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def f13_windows(today):
    out, s = [], F13_START
    while True:
        e = add_months(s, 3) - dt.timedelta(days=1)
        if e >= today - dt.timedelta(days=10):
            break
        name = f"{s.day:02d}{s.strftime('%b').lower()}{s.year}-{e.day:02d}{e.strftime('%b').lower()}{e.year}_form13f.zip"
        out.append((s, e, name))
        s = add_months(s, 3)
    return out


def sec_day(v):
    v = str(v or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def tsv_rows(zf, suffix):
    import zipfile  # noqa: F401
    name = next((n for n in zf.namelist() if n.upper().endswith(suffix)), None)
    if not name:
        raise RuntimeError(f"{suffix} missing in zip")
    f = io.TextIOWrapper(zf.open(name), encoding="utf-8", errors="replace")
    rd = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
    header = [h.strip().upper() for h in next(rd)]
    return header, rd


def fund_of(cusip, issuer, title):
    c = (cusip or "").strip().upper()
    if c in FUND_CUSIPS:
        return FUND_CUSIPS[c]
    u = f"{issuer} {title}".upper()
    if "GOLD" not in u or "MINER" in u:
        return None
    for key, rx in FUND_NAMES:
        if re.search(rx, u):
            return key
    return None


def process_f13_zip(path, hist):
    import zipfile
    csv.field_size_limit(10 ** 8)
    with zipfile.ZipFile(path) as zf:
        h, rd = tsv_rows(zf, "SUBMISSION.TSV")
        ix = {k: h.index(k) for k in ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT") if k in h}
        subs = {}
        for r in rd:
            try:
                subs[r[ix["ACCESSION_NUMBER"]]] = (r[ix["CIK"]].lstrip("0"), sec_day(r[ix["PERIODOFREPORT"]]),
                                                   r[ix["SUBMISSIONTYPE"]].upper(), sec_day(r[ix["FILING_DATE"]]) or "")
            except (IndexError, KeyError):
                continue
        h, rd = tsv_rows(zf, "COVERPAGE.TSV")
        gi = lambda k: h.index(k) if k in h else None  # noqa: E731
        a_i, n_i, t_i, r_i = gi("ACCESSION_NUMBER"), gi("FILINGMANAGER_NAME"), gi("AMENDMENTTYPE"), gi("REPORTTYPE")
        cover = {}
        for r in rd:
            try:
                cover[r[a_i]] = (r[n_i].strip() if n_i is not None else "",
                                 (r[t_i] if t_i is not None else "").upper(), (r[r_i] if r_i is not None else "").upper())
            except IndexError:
                continue
        h, rd = tsv_rows(zf, "INFOTABLE.TSV")
        g = {k: h.index(k) for k in ("ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE",
                                      "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL") if k in h}
        gold, px = {}, {}
        for r in rd:
            try:
                f = fund_of(r[g["CUSIP"]], r[g["NAMEOFISSUER"]], r[g["TITLEOFCLASS"]])
                if not f or r[g["PUTCALL"]].strip() or r[g["SSHPRNAMTTYPE"]].strip().upper() != "SH":
                    continue
                sh, val = num(r[g["SSHPRNAMT"]]), num(r[g["VALUE"]])
            except (IndexError, KeyError):
                continue
            if not sh or sh <= 0:
                continue
            acc = r[g["ACCESSION_NUMBER"]]
            gold.setdefault(acc, {})
            gold[acc][f] = gold[acc].get(f, 0) + sh
            sub = subs.get(acc)
            if sub and val:
                lst = px.setdefault(sub[1], {}).setdefault(f, [])
                if len(lst) < 400:
                    lst.append(val / sh)

    order = sorted(subs.items(), key=lambda kv: kv[1][3])
    for acc, (cik, period, typ, fdate) in order:
        if not period or not typ.startswith("13F-HR"):
            continue
        name, amend, rtype = cover.get(acc, ("", "", ""))
        if "NOTICE" in rtype:
            continue
        per = hist["periods"].setdefault(period, {"filers": {}, "hold": {}, "px": {}})
        per["filers"][cik] = max(per["filers"].get(cik, ""), fdate)
        if name:
            hist["names"][cik] = name
        funds = gold.get(acc, {})
        if typ == "13F-HR" or "RESTAT" in amend:
            if typ == "13F-HR" or funds:
                if funds:
                    per["hold"][cik] = {k: int(v) for k, v in funds.items()}
                else:
                    per["hold"].pop(cik, None)
        elif "NEW HOLD" in amend and funds:
            cur = per["hold"].setdefault(cik, {})
            for k, v in funds.items():
                cur[k] = int(cur.get(k, 0) + v)
    for period, d in px.items():
        per = hist["periods"].setdefault(period, {"filers": {}, "hold": {}, "px": {}})
        for f, lst in d.items():
            per["px"][f] = (per["px"].get(f, []) + lst)[-400:]
    return len(gold)


def gld_ozps_at(date, table):
    keys = sorted(k for k in table if k <= date[:7])
    if keys:
        return table[keys[-1]]
    years = (dt.date.fromisoformat(date) - dt.date(2004, 11, 18)).days / 365.25
    return 0.1 * (1 - 0.004) ** years                      # تقدير: الرسوم تخفض الذهب لكل سهم 0.4% سنويًا


def build_f13(hist, windows):
    ozt = load(GLD_OZPS, {})
    periods = sorted(hist["periods"])
    if not periods:
        return None
    maxh = max(len(hist["periods"][p]["hold"]) for p in periods)
    periods = [p for p in periods if len(hist["periods"][p]["hold"]) >= 0.3 * maxh]
    kg, conv = {}, {}
    for p in periods:
        per = hist["periods"][p]
        med = {f: statistics.median(v) for f, v in per["px"].items() if v}
        gld_oz = gld_ozps_at(p, ozt)
        gold_px = (med["GLD"] / gld_oz) if med.get("GLD") else None
        c = {"GLD": gld_oz}
        for f, m in med.items():
            if f != "GLD" and gold_px and 500 < gold_px < 30000:
                c[f] = m / gold_px
        conv[p] = c
        kg[p] = {cik: sum(sh * c.get(f, 0) * OZ_KG for f, sh in funds.items()) for cik, funds in per["hold"].items()}

    def val(p, cik):
        if cik in kg[p]:
            return kg[p][cik]
        return 0.0 if cik in hist["periods"][p]["filers"] else None

    def status(prev, cur):
        if cur is None:
            return None
        if prev is None:
            return "first" if cur > 0 else None
        if abs(cur - prev) < 0.5:
            return "same"
        if prev <= 0.5 and cur > 0:
            return "new"
        if prev > 0 and cur <= 0.5:
            return "closed"
        return "up" if cur > prev else "down"

    names, majors = hist["names"], re.compile(MAJORS)
    done = set(hist["done"])
    out_periods, keep = [], set()
    for i, p in enumerate(periods):
        prev_p = periods[i - 1] if i else None
        ciks = set(kg[p]) | (set(kg[prev_p]) if prev_p else set())
        moves = []
        for cik in ciks:
            cur = val(p, cik)
            prev = val(prev_p, cik) if prev_p else None
            st = status(prev, cur)
            if st in ("new", "up", "down", "closed") or (st == "first" and prev_p):
                moves.append({"c": cik, "n": names.get(cik, cik), "a": round(prev or 0), "b": round(cur), "s": st})
        buys = sorted([m for m in moves if m["b"] > m["a"]], key=lambda m: m["a"] - m["b"])
        sells = sorted([m for m in moves if m["b"] < m["a"]], key=lambda m: m["b"] - m["a"])
        deadline = dt.date.fromisoformat(p) + dt.timedelta(days=45)
        complete = any(s <= deadline <= e and n in done for s, e, n in windows)
        by_fund = {}
        for cik, funds in hist["periods"][p]["hold"].items():
            for f, sh in funds.items():
                by_fund[f] = by_fund.get(f, 0) + sh * conv[p].get(f, 0) * OZ_KG
        out_periods.append({
            "p": p, "total": round(sum(kg[p].values())), "holders": len(kg[p]), "complete": complete,
            "funds": {f: round(v) for f, v in by_fund.items()},
            "buy": round(sum(m["b"] - m["a"] for m in buys)), "sell": round(sum(m["a"] - m["b"] for m in sells)),
            "n": {k: sum(1 for m in moves if m["s"] == k) for k in ("new", "first", "up", "down", "closed")},
            "buyers": buys[:15], "sellers": sells[:15],
            "opened": [m for m in buys if m["s"] in ("new", "first")][:8], "closed": [m for m in sells if m["s"] == "closed"][:8],
        })
        for lst in (buys[:15], sells[:15]):
            keep.update(m["c"] for m in lst)
    peak = {}
    for p in periods:
        for cik, v in kg[p].items():
            peak[cik] = max(peak.get(cik, 0), v)
    keep.update(sorted(peak, key=lambda c: -peak[c])[:250])
    keep.update(c for c in peak if majors.search(names.get(c, "").upper()))
    managers = {}
    for cik in keep:
        ser, prev = [], None
        for p in periods:
            v = val(p, cik)
            ser.append([p, None if v is None else round(v), status(prev, v), hist["periods"][p]["hold"].get(cik, {})])
            if v is not None:
                prev = v
        managers[cik] = {"n": names.get(cik, cik), "m": bool(majors.search(names.get(cik, "").upper())), "s": ser}
    return {"periods": out_periods, "managers": managers, "conv": conv}


def fetch_f13():
    import tempfile
    hist = load(F13_HIST, {"done": [], "periods": {}, "names": {}})
    windows = f13_windows(dt.date.today())
    todo = [w for w in windows if w[2] not in hist["done"]]
    errors = []
    log(f"13F user-agent: {'contact from SEC_CONTACT secret' if '@' in os.environ.get('SEC_CONTACT', '') else sec_ua()} "
        f"(one line, {len(sec_ua())} chars)")
    for s, e, name in todo:
        path = os.path.join(tempfile.gettempdir(), name)
        try:
            code, where = 404, ""
            for base in F13_BASES:
                code = sec_download(base + name, path)
                if code == 200:
                    where = base.split("/")[2] + "/" + base.split("/")[4]
                    break
            if code == 404:
                log(f"13F {name}: not published yet")
                continue
            n = process_f13_zip(path, hist)
            os.unlink(path)
            hist["done"].append(name)
            save(F13_HIST, hist)
            log(f"13F {name}: {n} filings with gold ETF positions ({where})")
        except Exception as ex:
            errors.append(f"{name}: {ex}")
            log("13F", name, "FAILED:", ex)
            if len(errors) >= 2 and not hist["done"]:
                log("13F: stopping early, SEC is refusing every download")
                break
    out = build_f13(hist, windows)
    if not out:
        raise RuntimeError("13F: no data — " + " | ".join(errors)[:220])
    last = out["periods"][-1]
    log(f"13F ok: {len(out['periods'])} quarters, latest {last['p']} ({last['holders']} institutions, "
        f"{last['total'] / 1000:,.1f} t), {len(out['managers'])} managers kept")
    if errors:
        log("13F partial errors:", " | ".join(errors)[:300])
    try:
        out["types"] = classify_f13(out)
    except Exception as e:
        log("13F institution types FAILED:", str(e)[:200])
        out["types"] = load(F13_TYPES, {})
    return {"f13": out}


F13_KINDS = ("bank", "asset_manager", "hedge_fund", "private_equity", "wealth", "broker", "insurance", "pension",
             "sovereign", "endowment", "family_office", "other")
F13_TYPE_SYSTEM = """Classify each institution that files SEC Form 13F by its main business, from its name and your knowledge.
Categories: bank (commercial/universal bank or bank holding company, including its trust arm), asset_manager (mutual fund,
ETF or institutional asset manager), hedge_fund, private_equity (private equity, private credit, alternative asset manager),
wealth (registered investment adviser, private wealth or financial planning firm), broker (broker-dealer or market maker),
insurance, pension (pension or retirement fund), sovereign (central bank, sovereign wealth fund, government entity),
endowment (university endowment or charitable foundation), family_office, other.
Return ONLY one JSON object mapping each id to one category, e.g. {"123": "bank"}."""


def classify_f13(out):
    """نوع كل مؤسسة (بنك، صندوق تحوّط، مدير أصول...) يصنّفه Claude Haiku مرة واحدة من اسمها، ويُحفظ ليُعاد استخدامه."""
    types = load(F13_TYPES, {})
    names = {cik: m["n"] for cik, m in (out.get("managers") or {}).items()}
    for q in out.get("periods") or []:
        for key in ("buyers", "sellers", "opened", "closed"):
            for m in q.get(key) or []:
                names.setdefault(m["c"], m["n"])
    todo = [c for c in names if c not in types]
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if todo and key:
        for i in range(0, len(todo), 80):
            batch = todo[i:i + 80]
            # تصنيف بسيط: بلا تفكير مطوّل كي لا يستهلك حدّ Tokens قبل كتابة الجواب
            resp = _claude({"model": WGC_MODEL, "max_tokens": 8000, "thinking": {"type": "disabled"}, "system": F13_TYPE_SYSTEM,
                            "messages": [{"role": "user", "content": "\n".join(f"{c}|{names[c]}" for c in batch)}]}, key)
            txt = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            if "{" not in txt:
                raise RuntimeError(f"no JSON in reply (stop_reason {resp.get('stop_reason')})")
            got = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            types.update({c: (got.get(c) if got.get(c) in F13_KINDS else "other") for c in batch if c in got})
            save(F13_TYPES, types)
        log(f"13F institution types: classified {len(todo)} new institutions")
    return {c: types[c] for c in names if c in types}


# ---------------------------------------------------------------- AI daily report (Claude API)
AI_HIST = os.path.join("data", "ai_history.json")
AI_MODEL = os.environ.get("AI_MODEL", "claude-fable-5-1")
AI_RESEARCH_LOG = os.path.join("data", "ai_research_log.json")
OWNER_PROMPT = os.path.join("prompts", "fable.md")        # تعليمات صاحب المنصة، يعدّلها من GitHub مباشرة
# مرحلة الاكتشاف: Fable يبحث وحده (تقرير يوميًا بعد الإغلاق الأمريكي) ونسجّل كل بحث، لنعرف ما يحتاجه كل قسم
# قبل نقل البحث إلى نموذج أرخص. تنتهي بعد أول تقرير يطلقه COT جديد من DISCOVERY_COT_FROM فصاعدًا
# (تقرير الجمعة)، أو عند DISCOVERY_END إن تأخر COT. يوم الجمعة لا تقرير مجدول: ننتظر COT.
DISCOVERY_COT_FROM = "2026-10-30"
DISCOVERY_END = "2026-11-03"
DISCOVERY_HOUR_UTC = 21
DISCOVERY_SEARCHES = 8
AI_RETRY_HOURS = 6
# التقرير اليومي: أيام العمل الساعة 13:30 بتوقيت فيينا، حين تكون بيانات الأمس كلها قد وصلت (OI، ETF، COMEX...)
# وقبل افتتاح نيويورك بساعتين. التقارير الكبرى (COT وغيرها) لا تطلق تقريرًا فوريًا: تُجمع وتظهر في التقرير التالي.
VIENNA = ZoneInfo("Europe/Vienna")
DAILY_REPORT_AT = (13, 30)


class _AIWait(Exception):
    pass
AI_URL = "https://api.anthropic.com/v1/messages"
AI_LANGS = ("ar",)                      # المنصة بالعربية فقط
PRICES_PER_MTOK = (("claude-fable", 10, 50), ("claude-mythos", 10, 50), ("claude-opus-5-5", 4, 20), ("claude-opus", 5, 25),
                   ("claude-sonnet", 2, 10), ("claude-haiku-5-5", 0.1, 0.5), ("claude-haiku", 1, 5))


def _t(kg):
    return round(kg / 1000, 2)


def _cot_index(recs, weeks):
    r = recs[-weeks:]
    nets = [x[1] - x[2] for x in r]
    lo, hi = min(nets), max(nets)
    return round((nets[-1] - lo) / (hi - lo) * 100) if hi > lo else 50


def _flow(series, n):
    if len(series) <= n:
        return None
    return _t(series[-1][1] - series[-1 - n][1])


# ---------------------------------------------------------------- hidden correlation pack (for the AI only)
CORR_EXTRA = (("oil", "CL=F"), ("silver", "SI=F"), ("platinum", "PL=F"))
CORR_EXPECT = {"real_yield": -1, "dollar": -1, "oil": 1, "silver": 1, "platinum": 1, "nq": 0, "vix": 0}


def _pair_rets(g, f, kind):
    """تغيرات يومية للذهب (٪) وللعامل على التواريخ المشتركة فقط. kind: pct للأسعار، diff للعوائد وVIX."""
    ds = sorted(set(g) & set(f))
    out = []
    for a, b in zip(ds, ds[1:]):
        if g[a] and f[a]:
            x = (g[b] / g[a] - 1) * 100
            y = (f[b] / f[a] - 1) * 100 if kind == "pct" else f[b] - f[a]
            out.append((b, x, y))
    return out


def _corr(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    return round(sxy / math.sqrt(sxx * syy), 2) if sxx and syy else None


def _corr_state(c, sign, n=60):
    if c is None:
        return None
    cr = 1.96 / math.sqrt(n)
    if sign == 0:
        return "moves_with_gold" if c >= cr else "moves_against_gold" if c <= -cr else "no_clear_link"
    return "normal" if c * sign >= cr else "inverted" if c * sign <= -cr else "weak"


def correlation_pack(d):
    """ارتباطات الذهب المخفية: تُحسب لحظة كتابة التقرير وتُرسل إلى النموذج فقط، ولا تُحفظ في أي ملف منشور.
    ١) الذهب مع محرّكاته (20/60/250 جلسة) وحالة العلاقة، ٢) القيمة العادلة: كم تفسّر العوائد الحقيقية والدولار
    من حركة الذهب، والباقي علاوة غير مفسَّرة، ٣) اتساع المعادن: فضة + بلاتين بأوزان حسب التذبذب، ٤) نظام المخاطر."""
    gold = dict(load(PRICES_OUT, {}).get("daily") or [])
    m = d.get("macro") or {}
    fac = {"real_yield": (dict(m.get("real10") or []), "diff"), "dollar": (dict(m.get("dxy") or []), "pct"),
           "nq": (dict(m.get("nq") or []), "pct"), "vix": (dict(m.get("vix") or []), "diff")}
    start = dt.date.today() - dt.timedelta(days=640)
    for key, sym in CORR_EXTRA:
        try:
            fac[key] = (yahoo_daily(sym, start), "pct")
        except Exception as e:
            log(f"correlation: {sym} failed: {str(e)[:80]}")
    if len(gold) < 300:
        return None
    out = {"note": ("PRIVATE context for your reasoning only. Do not name these measures, the metals index or the model, "
                    "and do not quote these correlation or regression numbers in the report; use only the conclusions, "
                    "in plain words (e.g. 'gold is moving on its own, not with the dollar or real yields'). "
                    "Correlations use daily changes on shared trading days; |r| below 0.44 (20 days) or 0.25 (60 days) "
                    "can be chance. Correlation is not causation."),
           "drivers": {}}
    for k, (f, kind) in fac.items():
        r = _pair_rets(gold, f, kind)
        if len(r) < 30:
            continue
        xs, ys = [a[1] for a in r], [a[2] for a in r]
        c = {f"r{n}": _corr(xs[-n:], ys[-n:]) for n in (20, 60, 250) if len(xs) >= n}
        c["state_60d"] = _corr_state(c.get("r60"), CORR_EXPECT[k])
        c["normal_sign"] = {-1: "negative", 1: "positive", 0: "none (risk regime)"}[CORR_EXPECT[k]]
        c["as_of"] = r[-1][0]
        out["drivers"][k] = c
    # ٢) القيمة العادلة: انحدار متحرك (120 جلسة) لعوائد الذهب على تغير العائد الحقيقي وتغير الدولار،
    # بمعاملات اليوم السابق فقط، ثم مجموع آخر 20 جلسة: الفعلي مقابل المفسَّر
    ry, dxy = fac["real_yield"][0], fac["dollar"][0]
    ds = sorted(set(gold) & set(ry) & set(dxy))
    rows = [((gold[b] / gold[a] - 1) * 100, ry[b] - ry[a], (dxy[b] / dxy[a] - 1) * 100) for a, b in zip(ds, ds[1:])]
    rdates = ds[1:]
    W, S = 120, 20
    if len(rows) > W + S:
        def betas(win):
            n = len(win)
            my, m1, m2 = (sum(r[i] for r in win) / n for i in range(3))
            v1 = sum((r[1] - m1) ** 2 for r in win) / n
            v2 = sum((r[2] - m2) ** 2 for r in win) / n
            c12 = sum((r[1] - m1) * (r[2] - m2) for r in win) / n
            cy1 = sum((r[0] - my) * (r[1] - m1) for r in win) / n
            cy2 = sum((r[0] - my) * (r[2] - m2) for r in win) / n
            vy = sum((r[0] - my) ** 2 for r in win) / n
            den = v1 * v2 - c12 ** 2
            if not den or not vy:
                return None
            b1, b2 = (cy1 * v2 - cy2 * c12) / den, (cy2 * v1 - cy1 * c12) / den
            return b1, b2, (b1 * cy1 + b2 * cy2) / vy
        act = expl = 0.0
        for i in range(len(rows) - S, len(rows)):
            bt = betas(rows[i - W:i])
            if bt:
                act += rows[i][0]
                expl += bt[0] * rows[i][1] + bt[1] * rows[i][2]
        last = betas(rows[-W:])
        if last:
            out["fair_value_20d"] = {"window": f"{rdates[-S]} to {rdates[-1]}", "gold_actual_pct": round(act, 2), "explained_by_real_yield_and_dollar_pct": round(expl, 2),
                                     "unexplained_pct": round(act - expl, 2), "model_r2": round(last[2], 2),
                                     "gold_pct_per_1pp_real_yield": round(last[0], 1), "gold_pct_per_1pct_dollar": round(last[1], 2),
                                     "reading": ("positive unexplained = gold stronger than yields and the dollar justify "
                                                 "(official buying, haven, Asian demand); negative = weaker (positioning "
                                                 "liquidation, forced selling); low r2 = yields and the dollar are not driving gold now")}
    # ٣) اتساع المعادن: فضة + بلاتين، وزن كل منهما عكس تذبذبه في آخر 60 جلسة
    if "silver" in fac and "platinum" in fac:
        ag, pt = fac["silver"][0], fac["platinum"][0]
        ds = sorted(set(gold) & set(ag) & set(pt))
        rr = [((gold[b] / gold[a] - 1) * 100, (ag[b] / ag[a] - 1) * 100, (pt[b] / pt[a] - 1) * 100) for a, b in zip(ds, ds[1:])]
        mdates = ds[1:]
        if len(rr) > 80:
            sd = lambda xs: statistics.pstdev(xs) or None
            s_ag, s_pt = sd([r[1] for r in rr[-60:]]), sd([r[2] for r in rr[-60:]])
            if s_ag and s_pt:
                w_ag, w_pt = (1 / s_ag) / (1 / s_ag + 1 / s_pt), (1 / s_pt) / (1 / s_ag + 1 / s_pt)
                met = [w_ag * r[1] + w_pt * r[2] for r in rr]
                g = [r[0] for r in rr]
                out["metals_breadth"] = {"window_20d": f"{mdates[-20]} to {mdates[-1]}", "weights": {"silver": round(w_ag, 2), "platinum": round(w_pt, 2)},
                                         "gold_20d_pct": round(sum(g[-20:]), 2), "metals_20d_pct": round(sum(met[-20:]), 2),
                                         "gold_minus_metals_20d_pct": round(sum(g[-20:]) - sum(met[-20:]), 2),
                                         "corr_60d": _corr(g[-60:], met[-60:]),
                                         "reading": ("gold up while metals lag = gold-specific demand (central banks, haven); "
                                                     "all up together = broad speculative metals move")}
    return out


# ---------------------------------------------------------------- hidden nowcasts of lagging reports (for the AI only)
def _ols(rows):
    """انحدار خطي صغير: rows = [(y, x1, x2, ...)] -> [a, b1, b2, ...] (حل المعادلات الطبيعية بالحذف)."""
    k = len(rows[0])
    X = [[1.0] + list(r[1:]) for r in rows]
    y = [r[0] for r in rows]
    A = [[sum(X[n][i] * X[n][j] for n in range(len(X))) for j in range(k)] + [sum(X[n][i] * y[n] for n in range(len(X)))]
         for i in range(k)]
    for i in range(k):
        piv = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[piv] = A[piv], A[i]
        if abs(A[i][i]) < 1e-12:
            return None
        for r in range(k):
            if r != i:
                f = A[r][i] / A[i][i]
                A[r] = [a - f * b for a, b in zip(A[r], A[i])]
    return [A[i][k] / A[i][i] for i in range(k)]


def _p_below_zero(f, sd):
    return 0.5 * (1 + math.erf((0 - f) / (sd * math.sqrt(2)))) if sd else None


def nowcast_real_yield(d):
    """العائد الحقيقي (FRED DFII10) يصدر متأخرًا 1-2 يوم عمل؛ العائد الاسمي لعشر سنوات (^TNX) لحظي ويتحرك معه
    بارتباط ~0.9، فنقدّر قيمة اليوم من تغيّر الاسمي منذ آخر قيمة رسمية."""
    ry = dict((d.get("macro") or {}).get("real10") or [])
    if len(ry) < 300:
        return None
    tnx = yahoo_daily("%5ETNX", dt.date.today() - dt.timedelta(days=800))
    ds = sorted(set(ry) & set(tnx))
    ch = [(ry[b] - ry[a], tnx[b] - tnx[a]) for a, b in zip(ds, ds[1:])]
    if len(ch) < 300:
        return None
    fit = _ols(ch[-500:])
    if not fit:
        return None
    a, b = fit
    hits = n = 0
    errs = []
    for i in range(len(ch) - 250, len(ch)):             # دقة خارج العيّنة: نموذج يُدرَّب على ما قبل كل يوم فقط
        fw = _ols(ch[max(0, i - 500):i])
        if not fw:
            continue
        f = fw[0] + fw[1] * ch[i][1]
        errs.append(ch[i][0] - f)
        if ch[i][0]:
            n += 1
            hits += (f > 0) == (ch[i][0] > 0)
    last_off = max(ry)
    later = sorted(x for x in tnx if x > last_off)
    if not later:
        return {"official": {"value": ry[last_off], "as_of": last_off}, "nowcast": None,
                "note": "no newer nominal-yield data than the official real yield"}
    base_t = tnx.get(last_off) or tnx[max(x for x in tnx if x <= last_off)]
    days = len(later)
    est = ry[last_off] + days * a + b * (tnx[later[-1]] - base_t)
    sd = statistics.pstdev(errs) if errs else None
    return {"official": {"value": ry[last_off], "as_of": last_off},
            "nowcast": {"value": round(est, 3), "as_of": later[-1], "sessions_ahead": days,
                        "change_since_official_bp": round((est - ry[last_off]) * 100, 1),
                        "error_1sd_bp": round(sd * 100 * math.sqrt(days), 1) if sd else None},
            "method": "real-yield change estimated from the live 10-year nominal yield (^TNX)",
            "track_record_250d": {"direction_hit_rate": round(hits / n, 2) if n else None,
                                  "daily_error_1sd_bp": round(sd * 100, 1) if sd else None}}


def nowcast_cot(d):
    """تقدير تقرير COT القادم قبل صدوره: تغيّر صافي Long لدى Managed Money من ثلاثاء إلى ثلاثاء يُقدَّر من
    حركة السعر وتغيّر Open Interest. التدريب على 3 سنوات من الأسابيع، والتطبيق على CME اليومي منذ آخر ثلاثاء."""
    daily = load(PRICES_OUT, {}).get("daily") or []
    mm = (d.get("cot_disagg") or {}).get("mm") or []
    oi_w = {r[0]: r[4] for r in d.get("noncomm") or []}
    od = d.get("oi_daily") or []
    if len(mm) < 300 or not daily:
        return None
    px = lambda x: _price_at(daily, x)
    rows = []
    for a, b in zip(mm, mm[1:]):
        if a[0] in oi_w and b[0] in oi_w and px(a[0]) and px(b[0]):
            rows.append(((b[1] - b[2]) - (a[1] - a[2]), (px(b[0]) / px(a[0]) - 1) * 100, oi_w[b[0]] - oi_w[a[0]]))
    W = 156
    if len(rows) < W + 60:
        return None
    hits = n = 0
    errs = []
    for i in range(len(rows) - 104, len(rows)):          # آخر سنتين، خارج العيّنة
        fw = _ols(rows[i - W:i])
        if not fw:
            continue
        f = fw[0] + fw[1] * rows[i][1] + fw[2] * rows[i][2]
        errs.append(rows[i][0] - f)
        n += 1
        hits += (f > 0) == (rows[i][0] > 0)
    fit = _ols(rows[-W:])
    sd = statistics.pstdev(errs) if errs else None
    t0 = mm[-1][0]
    t1 = (dt.date.fromisoformat(t0) + dt.timedelta(days=7)).isoformat()
    later = [r for r in od if t0 < r[0] <= t1]
    base = next((r for r in od if r[0] == t0), None)
    last_px_day = max((r[0] for r in daily if t0 < r[0] <= t1), default=None)
    if not fit or not last_px_day:
        return {"last_cot_week_ending": t0, "nowcast": None, "note": "no price data since the last COT week yet"}
    dp = (px(last_px_day) / px(t0) - 1) * 100
    doi = (later[-1][1] - base[1]) if (later and base) else None
    if doi is None:                                       # بدون OI يومي: نموذج السعر وحده
        fit = _ols([(r[0], r[1]) for r in rows[-W:]])
        f = fit[0] + fit[1] * dp
    else:
        f = fit[0] + fit[1] * dp + fit[2] * doi
    net0 = mm[-1][1] - mm[-1][2]
    p_down = _p_below_zero(f, sd)
    return {"last_cot_week_ending": t0, "next_cot_week_ending": t1, "managed_money_net_last": net0,
            "inputs": {"price_change_pct": round(dp, 2), "price_through": last_px_day,
                       "cme_oi_change": doi, "oi_through": later[-1][0] if later else None},
            "nowcast": {"net_change_contracts": int(round(f)), "net_estimate": int(round(net0 + f)),
                        "error_1sd_contracts": int(round(sd)) if sd else None,
                        "p_net_long_reduced": round(p_down, 2) if p_down is not None else None,
                        "covers_full_week": bool(last_px_day >= t1)},
            "method": "Managed Money weekly net change regressed on gold price change and open-interest change (3 years)",
            "track_record_104w": {"direction_hit_rate": round(hits / n, 2) if n else None,
                                  "error_1sd_contracts": int(round(sd)) if sd else None}}


def nowcast_central_banks(d):
    """بيانات IMF تتأخر 2-3 أشهر ولا تُستنتج من الأسعار (اختبرناها: ارتباط 0.15)، فنعطي المعدل التاريخي للأشهر
    الناقصة، ونطلب من النموذج البحث عن المعلنين الأوائل."""
    cb = d.get("central_banks") or {}
    s = cb.get("series") or []
    if len(s) < 24:
        return None
    ch = [(b[1] - a[1]) / 1000 for a, b in zip(s, s[1:])]
    last = s[-1][0][:7]
    y, m = map(int, last.split("-"))
    today = dt.date.today()
    missing = []
    while (y, m) < (today.year, today.month):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        missing.append(f"{y}-{m:02d}")
    pos = sum(1 for c in ch if c > 0) / len(ch)
    return {"last_official_month": last, "months_not_yet_reported": missing,
            "base_rate_since": s[0][0][:7],
            "base_rate": {"share_of_months_net_buying": round(pos, 2), "mean_t_per_month": round(statistics.mean(ch), 1),
                          "sd_t": round(statistics.pstdev(ch), 1), "last_12_months_t": round(sum(ch[-12:]), 1)},
            "how_to_fill": ("search the early reporters for the missing months: People's Bank of China monthly reserve data "
                            "(around the 7th), National Bank of Poland, Central Bank of Turkey (weekly), Reserve Bank of India "
                            "(weekly statistical supplement), Czech National Bank, and the World Gold Council's monthly "
                            "central-bank statistics; compare what you find with the base rate")}


OZ_PER_TONNE = 32150.7


def nowcast_wgc_etf(d):
    """تقرير WGC الشهري لتدفقات صناديق الذهب العالمية يصدر بعد نهاية الشهر بأيام. الصناديق الأمريكية اليومية
    (GLD + GLDM) تتبعه بارتباط ~0.9 (اختبرناه على 12 شهرًا)، فنقدّر الشهر الذي لم يصدر بعد من تدفقها حتى الآن."""
    w = [x for x in d.get("wgc_etf") or [] if x.get("data_month")]
    gld, gldm = (d.get("etf") or {}).get("GLD") or [], (d.get("etf2") or {}).get("GLDM") or []
    daily = load(PRICES_OUT, {}).get("daily") or []
    if len(w) < 8 or not gld or not gldm or not daily:
        return None

    def last_upto(ser, month):
        v = [r for r in ser if r[0][:7] <= month]
        return v[-1] if v else None

    def prev_month(m):
        y, mo = map(int, m.split("-"))
        return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"

    def us_flow(m):
        a, b = last_upto(gld, prev_month(m)), last_upto(gld, m)
        c, e = last_upto(gldm, prev_month(m)), last_upto(gldm, m)
        px = [r[1] for r in daily if r[0][:7] == m]
        if not (a and b and c and e and px) or b[0][:7] != m:
            return None, None, None
        t = ((b[1] + e[1]) - (a[1] + c[1])) / 1000
        return t, t * statistics.mean(px) * OZ_PER_TONNE / 1e9, max(b[0], e[0])

    pts = {"global": [], "north_america": []}
    for x in w:
        u = us_flow(x["data_month"])
        if u[0] is None:
            continue
        if x.get("global_flow_usd_bn") is not None:
            pts["global"].append((x["global_flow_usd_bn"], u[1]))
        na_ = ((x.get("regions") or {}).get("north_america") or {}).get("flow_usd_bn")
        if na_ is not None:
            pts["north_america"].append((na_, u[1]))
    fits = {}
    for k, rows in pts.items():
        if len(rows) >= 8:
            f = _ols(rows)
            if f:
                err = statistics.pstdev([y - (f[0] + f[1] * x) for y, x in rows])
                hits = sum((y > 0) == (f[0] + f[1] * x > 0) for y, x in rows)
                fits[k] = (f, err, hits, len(rows))
    if "global" not in fits:
        return None
    last_m = max(x["data_month"] for x in w)
    today_m = dt.date.today().isoformat()[:7]
    est, m = [], last_m
    while m < today_m:
        y, mo = map(int, m.split("-"))
        m = f"{y + 1}-01" if mo == 12 else f"{y}-{mo + 1:02d}"
        u = us_flow(m)
        if u[0] is None:
            continue
        row = {"month": m, "us_funds_through": u[2], "month_complete": m < today_m,
               "us_gld_gldm_flow_t": round(u[0], 1), "us_gld_gldm_flow_usd_bn": round(u[1], 1)}
        for k, (f, err, hits, n) in fits.items():
            v = f[0] + f[1] * u[1]
            row[f"{k}_flow_usd_bn_estimate"] = round(v, 1)
            row[f"{k}_error_1sd_usd_bn"] = round(err, 1)
            row[f"{k}_p_outflow"] = round(_p_below_zero(v, err), 2) if err else None
        est.append(row)
    return {"last_wgc_month": last_m, "estimates": est,
            "method": "WGC monthly flow regressed on same-month US GLD+GLDM flow in US$",
            "track_record": {k: {"months": n, "direction_agreement": f"{hits}/{n}", "error_1sd_usd_bn": round(err, 1)}
                             for k, (f, err, hits, n) in fits.items()},
            "caveat": "short sample (about a year); a month in progress is only partly covered"}


def nowcast_pack(d):
    """التقديرات المبكرة للتقارير المتأخرة: تُحسب لحظة كتابة التقرير وتُرسل إلى النموذج فقط، ولا تُحفظ في أي ملف.
    دقة كل تقدير تُعاد حسابها في كل تقرير على أحدث البيانات (خارج العيّنة)، فتتصحح الثقة بها تلقائيًا."""
    out = {"note": ("PRIVATE estimates of reports that are not out yet. Treat them as estimates with the stated error, "
                    "never as official numbers; when the official figure exists, it wins. Do not name this model or quote "
                    "its estimates, probabilities or accuracy in the report; say it in plain words (e.g. 'early signs point "
                    "to more Managed Money selling in Friday's COT report'). Facts you find by search can be cited normally.")}
    for key, fn in (("real_yield", nowcast_real_yield), ("cot_managed_money", nowcast_cot),
                    ("central_banks", nowcast_central_banks), ("wgc_global_etf_flows", nowcast_wgc_etf)):
        try:
            r = fn(d)
            if r:
                out[key] = r
        except Exception as e:
            log(f"nowcast {key} FAILED:", repr(e)[:160])
    cpi = ((d.get("macro") or {}).get("fed") or {}).get("cpi")
    out["cpi"] = {"last_official": cpi, "how_to_fill": ("search the Federal Reserve Bank of Cleveland 'Inflation Nowcasting' "
                                                         "estimate for the current and next month's CPI and core CPI")}
    return out


def ai_digest(d):
    """ملخص مضغوط لكل أقسام المنصة يُرسل إلى Claude (أرقام فقط، دون بيانات شخصية)."""
    g = {}
    cb = d.get("central_banks") or {}
    s = cb.get("series") or []
    if len(s) > 13:
        g["central_banks"] = {"as_of": cb.get("as_of"), "total_t": _t(s[-1][1]), "change_1m_t": _t(s[-1][1] - s[-2][1]),
                              "change_3m_t": _t(s[-1][1] - s[-4][1]), "change_12m_t": _t(s[-1][1] - s[-13][1])}
    moves = []
    for c, ser in (d.get("countries") or {}).items():
        if len(ser) >= 2 and ser[-1][1] != ser[-2][1]:
            moves.append({"country": c, "month": ser[-1][0][:7], "change_t": _t(ser[-1][1] - ser[-2][1]), "holdings_t": _t(ser[-1][1])})
    moves.sort(key=lambda m: m["change_t"])
    g["country_moves_latest_month"] = {"top_buyers": moves[::-1][:6], "top_sellers": moves[:6]}
    cot = {}
    dis = d.get("cot_disagg") or {}
    for name, recs in (("managed_money", dis.get("mm")), ("swap_dealers", dis.get("swap")),
                       ("producer_merchant", dis.get("prod")), ("non_commercial", d.get("noncomm"))):
        if recs and len(recs) > 4:
            net = [x[1] - x[2] for x in recs]
            cot[name] = {"date": recs[-1][0], "long": recs[-1][1], "short": recs[-1][2], "net": net[-1],
                         "net_change_1w": net[-1] - net[-2], "net_change_3w": net[-1] - net[-4],
                         "cot_index_3y": _cot_index(recs, 156), "cot_index_6m": _cot_index(recs, 26)}
    g["cot_contracts"] = cot
    com = d.get("comex") or {}
    tot = com.get("total") or []
    if tot:
        g["comex_vaults"] = {"date": tot[-1][0], "total_t": _t(tot[-1][1]),
                             "registered_t": _t((com.get("registered") or [[0, 0]])[-1][1]),
                             "eligible_t": _t((com.get("eligible") or [[0, 0]])[-1][1]),
                             "change_1d_t": _flow(tot, 1), "change_5d_t": _flow(tot, 5), "change_20d_t": _flow(tot, 20)}
    dl = (d.get("deliveries") or {})
    days = sorted((dl.get("daily") or {}).keys())
    if days:
        last = dl["daily"][days[-1]]
        g["comex_deliveries_last_day"] = {"date": days[-1], "firms": {f: {k: {"issued": v[0], "stopped": v[1]} for k, v in acc.items()} for f, acc in last.items()}}
    if (dl.get("mtd") or {}).get("firms"):
        agg = []
        for f, acc in dl["mtd"]["firms"].items():
            i = sum(v[0] for v in acc.values()); st = sum(v[1] for v in acc.values())
            agg.append({"firm": f, "stopped": st, "issued": i, "net_contracts": st - i})
        agg.sort(key=lambda x: -abs(x["net_contracts"]))
        g["comex_deliveries_month_to_date"] = {"date": dl["mtd"].get("date"), "top": agg[:8]}
    etf = {}
    for k, ser in {**(d.get("etf2") or {}), **(d.get("etf") or {})}.items():
        if ser:
            etf[k] = {"date": ser[-1][0], "holdings_t": _t(ser[-1][1]), "flow_1d_t": _flow(ser, 1), "flow_5d_t": _flow(ser, 5), "flow_20d_t": _flow(ser, 20)}
    g["gold_etfs"] = etf
    ph = d.get("physical") or {}
    for key, label in (("efp", "comex_minus_london_usd_oz"), ("sh", "shanghai_premium_usd_oz")):
        rows = ph.get(key) or []
        if rows:
            vals = [r[1] for r in rows[-250:]]
            g[label] = {"date": rows[-1][0], "value": rows[-1][1], "avg_5d": round(sum(r[1] for r in rows[-5:]) / min(5, len(rows)), 2),
                        "percentile_1y": round(sum(1 for v in vals if v <= rows[-1][1]) / len(vals) * 100)}
    pr = load(PRICES_OUT, {})
    dly = pr.get("daily") or []
    if dly:
        def back(n):
            return dly[-1 - n][1] if len(dly) > n else None
        g["gold_price_usd_oz"] = {"last": (pr.get("last") or {}).get("price"), "last_time_utc": (pr.get("last") or {}).get("time"),
                                  "close": dly[-1][1], "close_date": dly[-1][0],
                                  "change_1w_pct": round((dly[-1][1] / back(5) - 1) * 100, 2) if back(5) else None,
                                  "change_1m_pct": round((dly[-1][1] / back(21) - 1) * 100, 2) if back(21) else None}
    if d.get("wgc_etf"):
        g["global_gold_etfs_wgc_latest_month"] = d["wgc_etf"][-1]
    mac = d.get("macro") or {}
    for key, label in (("real10", "us_10y_real_yield_tips_pct"), ("nom10", "us_10y_nominal_yield_pct"),
                       ("be10", "us_10y_breakeven_inflation_pct"), ("dxy", "dollar_index_dxy"), ("vix", "vix"),
                       ("nq", "nasdaq100_futures_nq")):
        ser = mac.get(key) or []
        if len(ser) > 22:
            g[label] = {"date": ser[-1][0], "value": ser[-1][1], "change_1w": round(ser[-1][1] - ser[-6][1], 3),
                        "change_1m": round(ser[-1][1] - ser[-22][1], 3)}
    fd = mac.get("fed") or {}
    if fd:
        g["fed_and_inflation"] = {k: fd.get(k) for k in ("fed_upper", "effr", "y2", "y2_minus_effr", "last_fed_change", "cpi", "core", "real_fed")}
        g["fed_and_inflation"]["last_24_months"] = fd.get("history", [])[-24:]
        g["fed_and_inflation"]["columns"] = "month, fed upper bound %, effective fed funds %, 2y yield %, CPI YoY %, effective minus CPI"
    if d.get("positioning"):
        g["futures_positioning"] = {k: v for k, v in d["positioning"].items() if k not in ("oi_series", "oi_daily_series", "ai_note")}
        if "mm_cost" in g["futures_positioning"]:
            g["futures_positioning"]["mm_cost"] = {k: v for k, v in d["positioning"]["mm_cost"].items() if k != "series"}
        g["futures_positioning"]["note"] = ("oi read: new_longs/new_shorts = fresh money, short_covering/long_liquidation = "
                                            "exits; flat_oi = no meaningful change; oi_daily_reads = CME daily (freshest); oi_1w/oi_3w = COT weekly, "
                                            "oi_change_dir excludes spread positions; who = COT weekly long/short/spread changes by group; "
                                            "verdict = freshest read vs COT; roll.warn = open interest distorted by contract roll; "
                                            "mm_cost = estimated average entry of Managed Money net long (approximate); "
                                            "concentration = % of open interest held by the largest 4/8 traders")
    tr = trend_and_base_rates()
    if tr:
        g["gold_trend_and_base_rates"] = tr
    try:
        cp = correlation_pack(d)
        if cp:
            g["hidden_correlations"] = cp
    except Exception as e:
        log("correlation pack FAILED:", repr(e)[:200])
    try:
        mirror = performance_mirror()
        if mirror:
            g["your_track_record"] = mirror
    except Exception as e:
        log("performance mirror FAILED:", repr(e)[:200])
    try:
        g["hidden_nowcasts"] = nowcast_pack(d)
    except Exception as e:
        log("nowcast pack FAILED:", repr(e)[:200])
    df = d.get("direction_filter") or {}
    if df.get("bands"):
        g["statistical_direction_filter"] = {
            **{k: df.get(k) for k in ("asof", "close", "day_ret", "z", "ma200", "ma50", "mom120", "regime", "signal",
                                      "sigma_pct", "sigma_usd", "bands", "levels", "gex", "invalidation")},
            "history": df.get("stats"), "live_scoring": {k: v for k, v in (df.get("track") or {}).items() if k != "rows"},
            "note": ("tested on gold since 2008: only signal=long_edge (down session while above MA200, next session up ~57%) "
                     "held in every era; signal=none means no statistical edge; bands = next-close range from 20-session "
                     "volatility with historical coverage; the GEX regime is not historically tested yet")}
    lq = d.get("liquidation") or {}
    if lq.get("today"):
        g["forced_selling_monitor"] = {"today": lq["today"], "thresholds": lq.get("params"),
                                       "history_since": lq.get("since"), "past_episodes_summary": lq.get("summary"),
                                       "last_episodes": lq.get("episodes", [])[-5:]}
    gx = d.get("gex") or {}
    if gx.get("spot"):
        g["gld_options_gamma"] = {k: gx.get(k) for k in ("asof", "spot", "total_m", "zero_all", "zero_near", "near_exp",
                                                         "call_wall", "put_wall", "gc_ratio")}
        g["gld_options_gamma"]["note"] = "GLD levels; multiply by gc_ratio for COMEX gold (GC) equivalents; total_m = USD millions per 1% move"
    tr = (d.get("outlook_track") or {}).get("summary")
    if tr and tr.get("n"):
        g["your_track_record"] = tr
    f = d.get("f13") or {}
    if f.get("periods"):
        q = f["periods"][-1]
        g["institutions_13f_latest_quarter"] = {
            "quarter_end": q["p"], "institutions": q["holders"], "total_t": _t(q["total"]), "bought_t": _t(q["buy"]), "sold_t": _t(q["sell"]),
            "counts": q["n"], "top_buyers": [{"name": m["n"], "change_t": _t(m["b"] - m["a"]), "status": m["s"]} for m in q["buyers"][:6]],
            "top_sellers": [{"name": m["n"], "change_t": _t(m["b"] - m["a"]), "status": m["s"]} for m in q["sellers"][:6]]}
    return g


AI_SYSTEM = """You are the senior gold-market analyst of a public, free, educational dashboard called "Gold Flows".
Write today's daily report from the platform data you are given, plus today's news found with web search.
Rules:
- Every number about flows, positions, holdings and spreads must come from the platform data. Never invent numbers.
- Use web search (up to {max_searches} searches) only for today's gold-relevant events: Federal Reserve and rates, US dollar and yields,
  geopolitics, central-bank gold news, China and India demand, major ETF or bank news. Cite each event's source URL.
  Real and nominal yields, breakeven inflation, the dollar index and GLD gamma levels are in the platform data: take those
  numbers from there, not from search results.
- Connect the sections: say when they agree or contradict each other (e.g. speculators crowded long while ETFs see outflows).
- Start your three probabilities from gold_trend_and_base_rates (the historical frequency of each outcome in similar
  conditions) and move them as far as your evidence justifies, naming the reasons. Read the trend (moving averages, distance
  from the 52-week high, down-day streaks, realized volatility) as well as the fundamentals.
- Use futures_positioning to say who is moving price: whether open interest shows fresh shorts or longs or exits, whether
  the largest traders are concentrating on one side, and whether Managed Money is selling at a profit or a loss versus its
  estimated average cost. Do not call a drop profit-taking unless this data supports it. Read it in layers: lead with the
  CME daily read (oi_daily_reads, freshest), then use the COT weekly 'who' to say which group moved; if they disagree, say
  so and give the date of each. Spread positions add open interest without direction (use oi_change_dir for COT).
- Use fed_and_inflation for policy: the Fed rate, the 2-year yield minus the effective rate as the market's early signal of
  cuts or hikes, CPI and core CPI (YoY, last month, 3-month annualized for acceleration) and the real Fed rate.
- When gold has fallen in recent sessions, judge whether the drop looks like forced selling to meet margin calls elsewhere
  (gold falling together with equities, VIX jumping, real yields not rising, the dollar bid) or a fundamental repricing
  (real yields or the dollar driving it). Use the forced_selling_monitor and its historical base rates, plus physical demand
  (Shanghai premium, EFP, ETF flows, central banks), and state how confident you are. A base rate is not a forecast.
- research_memory lists what changed in the platform data since the previous report, what that report already found by
  search, and when the next releases are due. Do not re-search known facts unless a newer release is due or you have a
  concrete reason to doubt them, and never search for numbers the platform data already gives (gold price, yields, the
  dollar, ETF holdings, COT, open interest). Spend your searches on what is new.
- your_track_record (present once at least 10 of your forecasts have been scored) shows how your past probabilities
  turned out. Correct systematic errors it reveals, but weigh its small, overlapping sample; never chase the last few results.
- hidden_nowcasts is private context: early estimates of lagging reports (the real yield before FRED publishes it, the
  next COT report, central-bank months the IMF has not reported, the next WGC global ETF flow figure, CPI). Use them to judge what is probably happening now,
  weighted by their stated error and track record; fill the central-bank and CPI gaps by searching the sources they name.
  Never present an estimate as an official figure, and never name these models or quote their numbers.
- hidden_correlations is private context: use its conclusions (which drivers are moving gold now, whether a usual link
  is broken, how much of the move yields and the dollar explain, whether other metals confirm), but never name these
  measures or quote their numbers in the report.
- statistical_direction_filter is shown publicly under your bias: a trend regime, a tested edge (or none), volatility bands
  and a levels map. Explain in one or two sentences how today's news and data fit it. If your 14-day bias points the other
  way from the regime, or you see an edge where it says none, say why. Its numbers may be quoted.
- focus_zones: pick the 3 or 4 zones that matter most for the next session from statistical_direction_filter.levels.
  Each zone is 1 to 3 of its level ids that sit close together (within about one sigma_usd). Use only ids from that
  list and never write prices yourself. role: "support", "resistance" or "pivot". why: one short Arabic line grounded in
  the data (e.g. which levels coincide and what the gamma regime implies there). These zones are drawn on the owner's chart.
- Be factual. This is not investment advice: no buy/sell recommendations, no price targets.
- After weighing every section and today's events, give your own analytical bias for gold over the next 14 calendar days
  and exactly three scenarios, one per direction, with estimated probabilities in whole percent that sum to 100, most likely
  first. The directions are scored objectively later against the COMEX gold close 14 days from now:
  "up" = more than 2% above today's price, "down" = more than 2% below, "flat" = within ±2%.
  Each scenario gets one short line on why, grounded in the data or events.
- Write the report in Arabic only, in correct Modern Standard Arabic (فصحى). Keep technical terms in English
  (COT, EFP, ETF, Long, Short, GEX, Gamma, Open Interest).
Return ONLY one JSON object, no markdown fences, with exactly this shape:
{"outlook": {"bias": "bullish|bearish|neutral", "scenarios": [{"dir": "up|flat|down", "p": 45}, {"dir": "...", "p": 30}, {"dir": "...", "p": 25}]},
 "ar": {"headline": "...", "tone": "إيجابي|سلبي|محايد|متباين", "summary": ["...", "..."],
        "sections": [{"title": "...", "body": "..."}], "events": [{"title": "...", "impact": "...", "source": "https://..."}],
        "watch": ["..."], "risks": "...",
        "outlook": {"bias_note": "one line on why", "scenarios": [{"title": "...", "why": "..."}, {...}, {...}]}},
 "focus_zones": [{"ids": ["put_2", "-1.5s"], "role": "support|resistance|pivot", "why": "سطر قصير بالعربية"}]}
The three scenarios follow the order of outlook.scenarios; each direction appears exactly once.
Also add a top-level "research_map" (English only, up to 10 items) describing what you searched for and why, so the
platform can learn which outside information each section needs:
 "research_map": [{"section": "macro|gex|countries|cb|banks|noncomm|signals|f13|comex|etf|physical|general",
                   "need": "what information was missing from the platform data", "queries": ["..."],
                   "sources": ["domain.com"], "found": "the key fact or number you used, or 'nothing useful'"}]
summary: 4 to 6 short points. sections: 4 to 6 (central banks and countries, banks and COT positioning, ETFs and institutions,
physical market and vaults, and anything else notable). events: 2 to 5. watch: 2 to 4 things to watch next."""


class ClaudeHTTPError(RuntimeError):
    def __init__(self, status, text):
        super().__init__(f"Claude API HTTP {status}: {text[:200]}")
        self.status = status


def _claude(payload, key, beta=None, timeout=300):
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if beta:
        headers["anthropic-beta"] = beta
    r = requests.post(AI_URL, json=payload, timeout=timeout, headers=headers)
    if r.status_code >= 400:
        raise ClaudeHTTPError(r.status_code, r.text)
    return r.json()


def _claude_stream(payload, key, beta=None, idle=300):
    """نفس _claude لكن بالبث (SSE): التقارير الطويلة لا تصطدم بمهلة كلية، والمهلة هنا بين دفعة وأخرى فقط.
    يعيد بناء الرسالة كاملة كما يعيدها الطلب العادي، فتُمرَّر كتلها كما هي في استكمال pause_turn."""
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if beta:
        headers["anthropic-beta"] = beta
    with requests.post(AI_URL, json={**payload, "stream": True}, timeout=(30, idle), headers=headers, stream=True) as r:
        if r.status_code >= 400:
            raise ClaudeHTTPError(r.status_code, r.text)
        msg, blocks, partial = None, {}, {}
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            d = json.loads(line[5:].strip())
            t = d.get("type")
            if t == "message_start":
                msg = d["message"]
            elif t == "content_block_start":
                blocks[d["index"]] = d["content_block"]
                partial[d["index"]] = ""
            elif t == "content_block_delta":
                b, dl = blocks[d["index"]], d["delta"]
                kind = dl.get("type")
                if kind == "text_delta":
                    b["text"] = (b.get("text") or "") + dl.get("text", "")
                elif kind == "input_json_delta":
                    partial[d["index"]] += dl.get("partial_json", "")
                elif kind == "thinking_delta":
                    b["thinking"] = (b.get("thinking") or "") + dl.get("thinking", "")
                elif kind == "signature_delta":
                    b["signature"] = dl.get("signature", "")
                elif kind == "citations_delta":
                    b["citations"] = (b.get("citations") or []) + [dl.get("citation")]
            elif t == "content_block_stop":
                if partial.get(d["index"]):
                    blocks[d["index"]]["input"] = json.loads(partial[d["index"]])
            elif t == "message_delta":
                msg.update(d.get("delta") or {})
                msg["usage"] = {**(msg.get("usage") or {}), **(d.get("usage") or {})}
            elif t == "error":
                err = d.get("error") or {}
                raise ClaudeHTTPError(529 if err.get("type") == "overloaded_error" else 500, json.dumps(d)[:300])
    if not msg:
        raise ClaudeHTTPError(500, "stream ended without a message")
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    return msg


def _usage_sum(parts):
    """مجموع الاستهلاك عبر كل استدعاءات الطلب الواحد: حين يتوقف البحث مؤقتًا (pause_turn) نكمله باستدعاء جديد
    يُعاد فيه إرسال السياق كله، وكل استدعاء يُحاسَب وحده."""
    t = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "web_search_requests": 0, "calls": len(parts)}
    for u in parts:
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            t[k] += u.get(k) or 0
        t["web_search_requests"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0) or 0
    return t


def _no_cost(ai):
    """يحذف بيانات الكلفة/التوكنز من التقرير قبل نشره (التقارير القديمة كانت تحفظها)."""
    if not ai:
        return ai
    ai = {k: v for k, v in ai.items() if k != "cost_30d"}
    ai["latest"] = {k: v for k, v in (ai.get("latest") or {}).items() if k != "usage"}
    ai["history"] = [{k: v for k, v in h.items() if k != "usage"} for h in ai.get("history") or []]
    return ai

def _cost(model, u):
    pin, pout = next(((i, o) for pre, i, o in PRICES_PER_MTOK if str(model).startswith(pre)), (10, 50))
    searches = u.get("web_search_requests", (u.get("server_tool_use") or {}).get("web_search_requests", 0)) or 0
    m = str(model)
    read = 0.025 if m.startswith(("claude-fable-5-1", "claude-mythos-5-1")) else 0.05 if m.startswith("claude-opus-5-5") else 0.1
    return (u.get("input_tokens", 0) * pin + (u.get("cache_creation_input_tokens") or 0) * pin * 1.25
            + (u.get("cache_read_input_tokens") or 0) * pin * read + u.get("output_tokens", 0) * pout) / 1e6 + searches * 0.01


def _outlook(rep):
    """الانحياز والاحتمالات الثلاثة: نتحقق من شكلها، ونسقطها بدل إسقاط التقرير كله إن جاءت ناقصة."""
    o = rep.get("outlook") or {}
    dirs = None
    if isinstance(o.get("scenarios"), list):
        sc = [x for x in o["scenarios"] if isinstance(x, dict)]
        dirs = [x.get("dir") for x in sc]
        p = [x.get("p") for x in sc]
        if sorted(map(str, dirs)) != ["down", "flat", "up"]:
            dirs = None                                 # اتجاهات غير واضحة: نعرض الاحتمالات ولا نقيّمها
    else:
        p = o.get("probabilities")
    if o.get("bias") not in ("bullish", "bearish", "neutral") or not isinstance(p, list) or len(p) != 3:
        return None
    try:
        p = [max(0.0, float(x)) for x in p]
    except (TypeError, ValueError):
        return None
    if not sum(p):
        return None
    p = [round(x * 100 / sum(p)) for x in p]
    p[0] += 100 - sum(p)                                    # التقريب قد يترك فرق 1٪
    for lang in AI_LANGS:
        lo = (rep.get(lang) or {}).get("outlook") or {}
        sc = lo.get("scenarios")
        if lang == "ar" and not (isinstance(sc, list) and len(sc) >= 3):
            return None
        if isinstance(rep.get(lang), dict) and isinstance(sc, list):
            rep[lang]["outlook"] = {"bias_note": str(lo.get("bias_note") or ""),
                                    "scenarios": [{"title": str(x.get("title") or ""), "why": str(x.get("why") or "")}
                                                  for x in sc[:3] if isinstance(x, dict)]}
    return {"bias": o["bias"], "p": p, **({"dirs": dirs} if dirs else {})}


def _owner_prompt():
    """نص prompts/fable.md بدون تعليقات HTML، أو فارغ إن لم يوجد الملف."""
    try:
        with open(OWNER_PROMPT, encoding="utf-8") as f:
            txt = re.sub(r"<!--.*?-->", "", f.read(), flags=re.S).strip()
    except OSError:
        return ""
    return txt[:8000]


def research_memory(prev_ai, releases, now):
    """ما يعرفه النموذج من التقرير السابق، حتى لا يصرف البحث على إعادة اكتشافه: ما جدّ في بيانات المنصة منذ آخر
    تقرير، وما وجده بالبحث آخر مرة، ومواعيد الإصدارات القادمة. توجيه لا منع: يبقى له أن يبحث إن كان لديه سبب."""
    last = (prev_ai or {}).get("latest") or {}
    since = last.get("generated_utc")
    if not since:
        return None
    new_data = sorted({k for k, rel in (releases or {}).items() for d, seen in (rel or {}).items() if seen and seen > since})
    rlog = load(AI_RESEARCH_LOG, [])
    found = []
    if rlog and rlog[-1].get("generated_utc") == since:
        found = [{k: m.get(k) for k in ("section", "need", "found", "sources")} for m in rlog[-1].get("research_map") or []]
    events = [{"title": e.get("title"), "source": e.get("source")} for e in (last.get("ar") or last.get("en") or {}).get("events") or []]
    nm = (now.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    fri = now.date() + dt.timedelta(days=(4 - now.weekday()) % 7)
    return {"previous_report_utc": since,
            "platform_data_new_since_then": new_data,
            "found_by_search_last_time": found,
            "events_in_last_report": events,
            "next_releases": {
                "COT": f"Friday {fri.isoformat()}, 15:30 New York time",
                "PBoC monthly reserves": f"around {nm.date().replace(day=7).isoformat()}",
                "WGC monthly ETF flows": f"around {nm.date().replace(day=5).isoformat()} to {nm.date().replace(day=8).isoformat()}",
                "US CPI": "mid-month (check the BLS schedule only if it matters today)",
                "IMF central-bank data": "monthly, two to three months behind"},
            "rule": ("These facts were already found or are already in the platform data. Do not spend searches finding them "
                     "again unless a newer release is due, the release date has passed, or you have a concrete reason to doubt "
                     "them; you may still cite them with their date and source. Spend searches on what is new since the "
                     "previous report.")}


def ai_report(data, prev, new_major, releases):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    prev_ai = prev.get("ai") or {}
    if not key:
        log("AI report skipped: no ANTHROPIC_API_KEY secret")
        return prev_ai or None, False
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    force = os.environ.get("AI_FORCE", "").strip().lower() in ("1", "true", "yes")
    if not force and not new_major:
        log("AI report: no new major report (COT, IMF, 13F, WGC) since the last one; no tokens used")
        return prev_ai or None, False
    log("AI report triggered by:", ", ".join(new_major) if new_major else "manual request")
    digest_obj = ai_digest(data)
    digest_obj["new_reports_triggering_this_update"] = {k: {"data_date": v, "first_seen_utc": (releases.get(k) or {}).get(v)}
                                                        for k, v in (new_major or {}).items() if k not in ("discovery", "daily")}
    try:
        mem = research_memory(prev_ai, releases, now)
        if mem:
            digest_obj["research_memory"] = mem
    except Exception as e:
        log("research memory FAILED:", repr(e)[:160])
    digest = json.dumps(digest_obj, ensure_ascii=False, separators=(",", ":"))
    user = (f"Date (UTC): {today}.\nPlatform data (JSON; t = tonnes, contracts = COMEX GC 100-oz contracts):\n{digest}\n\n"
            "Search for today's key gold-related events, then return the JSON report.")
    messages = [{"role": "user", "content": user}]
    sources, text, resp, spent, turns = [], "", None, [], []
    discovery = "discovery" in (new_major or {})
    n_search = DISCOVERY_SEARCHES if discovery else 4
    ws_new = [{"type": "web_search_20260209", "name": "web_search", "max_uses": n_search}]
    ws_old = [{"type": "web_search_20250305", "name": "web_search", "max_uses": n_search}]
    system = AI_SYSTEM.replace("{max_searches}", str(n_search))
    owner = _owner_prompt()
    if owner:
        system += ("\n\nStanding instructions from the platform owner (written in Arabic). Follow them within the rules and "
                   "the JSON shape above; if they ever conflict, the rules above win:\n" + owner)
    # fallbacks "default": إن رفض النموذج الطلب يُعاد تشغيله على نموذج بديل من Anthropic تلقائيًا (ويُعرض اسمه الحقيقي)
    use_cache = True
    attempts = [(ws_new, True), (ws_old, True), (ws_new, False), (None, False)]
    while attempts:
        tools, fallback = attempts[0]
        try:
            # Fable: التفكير دائمًا مفعّل ويُحسب من max_tokens، فنعطيه مساحة أكبر؛ العمق يُضبط بـ effort فقط
            # التخزين المؤقت (5 دقائق): كل دورة داخلية يعيد فيها النموذج قراءة السياق تُحسب قراءةً من الذاكرة
            # بجزء صغير من السعر بدل السعر الكامل؛ المحتوى الذي يراه النموذج لا يتغير
            payload = {"model": AI_MODEL, "max_tokens": 64000, "messages": list(messages), "output_config": {"effort": "high"}}
            if use_cache:
                payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                payload["cache_control"] = {"type": "ephemeral"}
            else:
                payload["system"] = system
            if tools:
                payload["tools"] = tools
            if fallback:
                payload["fallbacks"] = "default"
            beta = "server-side-fallback-2026-07-01" if fallback else None
            resp = _claude_stream(payload, key, beta)
            spent.append(resp.get("usage") or {})
            turns = [resp]
            for _ in range(3):                       # البحث الطويل قد يتوقف مؤقتًا (pause_turn) فنكمله
                if resp.get("stop_reason") != "pause_turn":
                    break
                payload["messages"] = payload["messages"] + [{"role": "assistant", "content": resp["content"]}]
                resp = _claude_stream(payload, key, beta)
                spent.append(resp.get("usage") or {})
                turns.append(resp)
            break
        except ClaudeHTTPError as e:
            if not 400 <= e.status < 500 or e.status == 429:
                raise                                # خطأ خادم أو ضغط: نعيد المحاولة في التشغيل التالي بدل تكرار طلب مكلف
            resp = None
            if use_cache and "cach" in str(e).lower():
                use_cache = False                    # التخزين المؤقت مرفوض: نعيد نفس المحاولة بدونه
                log("AI request: caching rejected, retrying without it:", str(e)[:160])
                continue
            log("AI request rejected" + (" (with web search)" if tools else "") + (" (with fallbacks)" if fallback else "") + ":", str(e)[:200])
            attempts.pop(0)
    if not resp:
        raise RuntimeError("Claude API: all attempts failed")
    if resp.get("stop_reason") == "refusal":
        raise RuntimeError(f"Claude API: request declined ({(resp.get('stop_details') or {}).get('category')})")
    queries = []
    for t in turns:                                  # كل الاستدعاءات، لا الأخير فقط: البحث قد يقع قبل pause_turn
        for b in t.get("content", []):
            if b.get("type") == "server_tool_use" and b.get("name") == "web_search":
                queries.append((b.get("input") or {}).get("query", ""))
            elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                for it in b["content"]:
                    u = it.get("url", "")
                    if u.startswith("https://") and u not in [x["url"] for x in sources]:
                        sources.append({"url": u, "title": (it.get("title") or "")[:140]})
    for b in resp.get("content", []):
        if b.get("type") == "text":
            text += b.get("text", "")
    i, j = text.find("{"), text.rfind("}")
    try:
        rep = json.loads(text[i:j + 1])
        if not isinstance(rep.get("ar"), dict):
            raise ValueError("missing ar")
    except Exception as e:
        raise RuntimeError(f"AI report: could not parse JSON ({e}); first chars: {text[:120]!r}")
    outlook = _outlook(rep)
    if not outlook:
        log("AI report: bias/scenarios missing or malformed; report kept without them")
    u = _usage_sum(spent)
    model = resp.get("model", AI_MODEL)
    # من كتب التقرير فعلًا: كتلة fallback تحدد نقطة التحويل، وusage.iterations يذكر كل محاولة ونموذجها
    for t in turns:
        for b in t.get("content", []):
            if b.get("type") == "fallback":
                model = ((b.get("to") or {}).get("model")) or model
                log(f"AI fallback: {(b.get('from') or {}).get('model')} -> {(b.get('to') or {}).get('model')}")
        its = (t.get("usage") or {}).get("iterations") or []
        if its:
            log("AI served-by:", ", ".join(f"{i.get('type')}:{i.get('model', '?')}" for i in its))
    cost = _cost(model, u)
    langs = {k: rep[k] for k in AI_LANGS if isinstance(rep.get(k), dict)}  # it/es اختياريتان: الواجهة تعرض الإنجليزية إن غابتا
    latest = {"date": today, "generated_utc": now.replace(microsecond=0).isoformat(), "model": model,
              **langs, "outlook": outlook, "sources": sources[:10]}
    fz = _focus_zones(rep.get("focus_zones"), digest_obj.get("statistical_direction_filter"))
    if fz:
        latest["focus"] = fz  # الكلفة تُكتب في سجل Actions فقط، لا في الملفات المنشورة
    last_price = load(PRICES_OUT, {}).get("last") or {}
    if outlook and outlook.get("dirs") and last_price.get("price"):
        olog = load(OUTLOOK_LOG, [])
        br = (digest_obj.get("gold_trend_and_base_rates") or {}).get("base_rates_10_sessions") or {}
        br = br.get("same_week_bucket_and_ma200_side") or next(iter(br.values()), None)
        olog.append({"id": latest["generated_utc"], "date": today, "model": model, "bias": outlook["bias"], "p": outlook["p"],
                     "dirs": outlook["dirs"], "base": {"date": str(last_price.get("time") or today)[:10], "price": last_price["price"]},
                     **({"base_rates": {k: br[k] for k in ("up", "flat", "down")}} if br else {})})
        save(OUTLOOK_LOG, olog[-300:])
    hist = load(AI_HIST, [])
    hist = [h for h in hist if h.get("generated_utc") != latest["generated_utc"]] + [latest]
    hist = hist[-30:]
    save(AI_HIST, hist)
    rlog = load(AI_RESEARCH_LOG, [])
    rlog.append({"generated_utc": latest["generated_utc"], "model": model, "trigger": list((new_major or {}).keys()) or ["manual"],
                 "queries": queries, "results": sources[:40], "research_map": rep.get("research_map") or []})
    save(AI_RESEARCH_LOG, rlog[-30:])
    log(f"AI report ok: {today}, model {latest['model']}, {u['calls']} calls, {u['input_tokens']} in / {u['output_tokens']} out tokens, "
        f"cache {u['cache_creation_input_tokens']} written / {u['cache_read_input_tokens']} read{'' if use_cache else ' (caching off)'}, "
        f"{u['web_search_requests']} searches, est. ${cost:.3f}")
    latest["trigger"] = list((new_major or {}).keys()) or ["manual"]
    hist[-1]["trigger"] = latest["trigger"]
    save(AI_HIST, hist)
    return {"latest": latest,
            "history": [{"date": h["date"], **{k: h[k] for k in AI_LANGS if k in h}, "outlook": h.get("outlook"),
                                           "sources": h.get("sources", []),
                                           "model": h.get("model"), "generated_utc": h.get("generated_utc"),
                                           "trigger": h.get("trigger", [])} for h in hist[-14:]]}, True


# ---------------------------------------------------------------- positioning: OI read, concentration, MM cost basis
def _price_at(daily, d):
    i = bisect.bisect_right([r[0] for r in daily], d) - 1
    return daily[i][1] if i >= 0 else None


OI_FLAT_PCT = 0.2          # تغيّر Open Interest أقل من 0.2٪ لا يُعدّ دخول أموال ولا خروجها
GC_MONTHS = (2, 4, 6, 8, 10, 12)


def _oi_kind(dp, doi, oi):
    if oi and abs(doi) / oi * 100 < OI_FLAT_PCT:
        return "flat_oi"
    return ("new_longs" if doi > 0 else "short_covering") if dp > 0 else ("new_shorts" if doi > 0 else "long_liquidation")


def gc_roll(today=None):
    """أول يوم إشعار (FND) لعقد GC النشط: آخر يوم عمل في الشهر السابق لشهر التسليم. قبله بثلاثة أسابيع يدوّر
    المتداولون مراكزهم إلى العقد التالي فيتشوّه تغيّر Open Interest."""
    today = today or dt.date.today()
    for y in (today.year, today.year + 1):
        for m in GC_MONTHS:
            fnd = dt.date(y, m, 1) - dt.timedelta(days=1)
            while fnd.weekday() > 4:
                fnd -= dt.timedelta(days=1)
            if fnd >= today:
                days = (fnd - today).days
                return {"contract": f"{y}-{m:02d}", "fnd": fnd.isoformat(), "days": days, "warn": days <= 21}


def positioning(d):
    """قراءة Open Interest مع السعر، وتغيّر تركّز أكبر المتداولين، ومتوسط تكلفة صافي Long لدى Managed Money.
    التكلفة: كلما زاد صافي Long يُضاف الجزء الجديد بسعر الذهب يوم التقرير (متوسط مرجّح)، ولا تتغير عند التقليص،
    وتبدأ من جديد إن انقلب الصافي إلى Short. تقدير تقريبي: لا نعرف أسعار تنفيذهم الفعلية."""
    daily = load(PRICES_OUT, {}).get("daily") or []
    nc, mm = d.get("noncomm") or [], (d.get("cot_disagg") or {}).get("mm") or []
    if len(nc) < 5 or not daily:
        return None
    def oi_read(a, b):
        # مراكز Spread (شراء شهر وبيع آخر) ترفع Open Interest بلا اتجاه، فتُطرح قبل الحكم
        pa, pb = _price_at(daily, a[0]), _price_at(daily, b[0])
        dp, doi, dsp = pb / pa - 1, b[4] - a[4], b[3] - a[3]
        return {"from": a[0], "to": b[0], "price_pct": round(dp * 100, 2), "oi_change": doi, "spread_change": dsp,
                "oi_change_dir": doi - dsp, "oi": b[4], "read": _oi_kind(dp, doi - dsp, b[4])}
    out = {"oi_1w": oi_read(nc[-2], nc[-1]), "oi_3w": oi_read(nc[-4], nc[-1]),
           "oi_series": [[r[0], r[4], _price_at(daily, r[0])] for r in nc[-104:]]}
    cost, net0, ser = None, 0, []
    for r in mm[-260:]:
        net, px = r[1] - r[2], _price_at(daily, r[0])
        if px is None:
            continue
        if net <= 0:
            cost = None
        elif cost is None or net0 <= 0:
            cost = px
        elif net > net0:
            cost = (cost * net0 + px * (net - net0)) / net
        net0 = net
        ser.append([r[0], net, round(cost, 1) if cost else None, px])
    if ser and ser[-1][2]:
        last_px = (load(PRICES_OUT, {}).get("last") or {}).get("price") or daily[-1][1]
        adds = [(ser[i][1] - ser[i - 1][1], ser[i][3]) for i in range(max(1, len(ser) - 13), len(ser)) if ser[i][1] > ser[i - 1][1]]
        recent = round(sum(a * px for a, px in adds) / sum(a for a, _ in adds), 1) if adds else None
        out["mm_cost"] = {"date": ser[-1][0], "net": ser[-1][1], "avg_cost": ser[-1][2], "price_now": last_px,
                          "pnl_pct": round((last_px / ser[-1][2] - 1) * 100, 2),
                          "recent_cost_13w": recent, "recent_added_13w": sum(a for a, _ in adds),
                          "recent_pnl_pct": round((last_px / recent - 1) * 100, 2) if recent else None, "series": ser[-60:]}
    ex = (d.get("cot_extra") or {})
    if ex.get("rows"):
        cols, rows = ex["columns"], ex["rows"]
        get = lambda row, c: row[1 + cols.index(c)]
        out["concentration"] = {c: {"now": get(rows[-1], c), "4w_ago": get(rows[-5], c) if len(rows) > 4 else None}
                                for c in cols}
        out["concentration"]["date"] = rows[-1][0]
    od = d.get("oi_daily") or []
    if len(od) > 1:
        a, b = od[-2], od[-1]
        out["oi_daily_last"] = {"date": b[0], "oi": b[1], "change": b[1] - a[1], "volume": b[2],
                                "price_pct": round((_price_at(daily, b[0]) / _price_at(daily, a[0]) - 1) * 100, 2)}
        # الطبقة ١ (الآن): CME اليومي لآخر جلسة و5 و15 جلسة
        reads = []
        for n in (1, 5, 15):
            if len(od) > n:
                a = od[-1 - n]
                dp = _price_at(daily, b[0]) / _price_at(daily, a[0]) - 1
                reads.append({"sessions": n, "from": a[0], "to": b[0], "price_pct": round(dp * 100, 2), "oi_change": b[1] - a[1],
                              "oi": b[1], "avg_volume": round(sum(x[2] for x in od[-n:]) / n), "read": _oi_kind(dp, b[1] - a[1], b[1])})
        out["oi_daily_reads"] = reads
        out["oi_daily_series"] = [[x[0], x[1], _price_at(daily, x[0])] for x in od[-60:]]
    # الطبقة ٢ (التأكيد): من غيّر مراكزه في آخر أسبوع COT
    cats = {"mm": mm, "swap": (d.get("cot_disagg") or {}).get("swap") or [], "prod": (d.get("cot_disagg") or {}).get("prod") or []}
    who = {}
    for k, r in cats.items():
        if len(r) > 1:
            a, b = r[-2], r[-1]
            who[k] = {"long": b[1] - a[1], "short": b[2] - a[2], "spread": (b[3] - a[3]) if len(b) > 3 else 0,
                      "net": (b[1] - b[2]) - (a[1] - a[2])}
    if who:
        out["who"] = {"from": nc[-2][0], "to": nc[-1][0], **who}
    # الحكم: من أحدث طبقة، مع اتفاقها أو خلافها مع COT
    dr = out.get("oi_daily_reads") or []
    prim = next((x for x in dr if x["sessions"] == 5), dr[0] if dr else None)
    if prim:
        out["verdict"] = {"source": "cme", "sessions": prim["sessions"], "to": prim["to"], "read": prim["read"],
                          "cot_read": out["oi_1w"]["read"], "cot_to": out["oi_1w"]["to"], "agree": prim["read"] == out["oi_1w"]["read"]}
    else:
        out["verdict"] = {"source": "cot", "to": out["oi_1w"]["to"], "read": out["oi_1w"]["read"]}
    out["roll"] = gc_roll()
    return out


POS_NOTE_SYSTEM = """You write ONE short sentence (max 28 words) in Arabic reading COMEX gold futures positioning for a dashboard.
Data has three layers: (1) CME daily open interest with price (freshest, 1/5/15 sessions; cannot split spreads),
(2) COT weekly 'who': changes in long/short/spread for managed_money (mm), swap dealers, producers (3 days old),
(3) mm_cost: estimated average entry of the Managed Money net long vs today's price.
Reads: new_longs, new_shorts = fresh money; short_covering, long_liquidation = exits; flat_oi = no meaningful change.
In COT reads, oi_change_dir already removes spread positions, which add open interest without direction.
Rules: lead with the freshest layer and say its date; say whether COT agrees and who moved; mention mm_cost only if it changes
the meaning (e.g. recent buyers under water = loss-cutting). If roll.warn is true, say open-interest changes are distorted by the roll.
Never write internal codes or field names (flat_oi, long_liquidation, oi_change_dir, mm_cost, roll.warn...): say them in plain words
(e.g. "open interest barely changed", "longs exiting"). Never invent causes or news, never call selling profit-taking unless mm_cost shows profit, no trade advice, no hype.
Arabic in Modern Standard Arabic, keep terms like Open Interest, COT, Managed Money, Long, Short, Spread in English.
Return only JSON: {"ar": "..."}"""


POS_NOTE_V = 3              # رفعه يعيد كتابة السطر عند تغيير التعليمات


def positioning_note(pos, prev_note):
    """سطر رأي Claude لهذا القسم: يُكتب فقط حين تصل بيانات يومية أو أسبوعية جديدة (Haiku، بلا بحث)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    stamp = f"{(pos.get('oi_daily_last') or {}).get('date')}|{pos['oi_1w']['to']}"
    force = os.environ.get("AI_FORCE", "").strip().lower() in ("1", "true", "yes")
    if prev_note and prev_note.get("stamp") == stamp and prev_note.get("v") == POS_NOTE_V and not force:
        return prev_note
    if not key:
        return prev_note
    data = {k: v for k, v in pos.items() if k not in ("oi_series", "oi_daily_series", "concentration", "ai_note")}
    if "mm_cost" in data:
        data["mm_cost"] = {k: v for k, v in data["mm_cost"].items() if k != "series"}
    resp = _claude({"model": WGC_MODEL, "max_tokens": 1500, "thinking": {"type": "disabled"}, "system": POS_NOTE_SYSTEM,
                    "messages": [{"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}]}, key)
    txt = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    got = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    if not all(isinstance(got.get(k), str) and got[k].strip() for k in AI_LANGS):
        raise RuntimeError("positioning note: missing languages")
    u = resp.get("usage") or {}
    log(f"positioning note ok ({stamp}): {u.get('input_tokens')} in / {u.get('output_tokens')} out tokens, est. ${_cost(WGC_MODEL, u):.4f}")
    return {"stamp": stamp, "v": POS_NOTE_V, "generated_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "model": resp.get("model", WGC_MODEL), **{k: got[k].strip() for k in AI_LANGS}}


# ---------------------------------------------------------------- data checks before publishing
def series_issues(label, s, lo=None, hi=None, jump=None, jump_abs=None, max_gap=None, tail=30):
    """أخطاء منطقية في آخر نقاط سلسلة [[تاريخ، قيمة], ...]: قيمة خارج المدى، قفزة غير معقولة، فجوة تواريخ، ترتيب أو تاريخ مستقبلي."""
    if not s:
        return [f"{label}: empty"]
    t, out = s[-tail:], []
    v = t[-1][1]
    if lo is not None and not lo <= v <= hi:
        out.append(f"{label}: latest value {v} outside {lo}..{hi}")
    if t[-1][0][:10] > (dt.date.today() + dt.timedelta(days=2)).isoformat():
        out.append(f"{label}: date in the future ({t[-1][0]})")
    for a, b in zip(t, t[1:]):
        gap = (dt.date.fromisoformat(b[0][:10]) - dt.date.fromisoformat(a[0][:10])).days
        if gap <= 0:
            out.append(f"{label}: dates out of order at {b[0]}")
        elif max_gap and gap > max_gap:
            out.append(f"{label}: {gap}-day gap before {b[0]}")
        elif jump and a[1] and abs(b[1] / a[1] - 1) > jump:
            out.append(f"{label}: {(b[1] / a[1] - 1) * 100:+.1f}% jump on {b[0]}")
        elif jump_abs and abs(b[1] - a[1]) > jump_abs:
            out.append(f"{label}: {b[1] - a[1]:+.2f} jump on {b[0]}")
        else:
            continue
        break
    return out


def check_data(name, d):
    """فحص ما جُلب للتو قبل نشره. أي مشكلة تعني: نبقي آخر بيانات سليمة ونُظهر تحذيرًا في القسم."""
    p = []
    if name == "comex":
        c = d.get("comex") or {}
        p += series_issues("COMEX total", c.get("total"), 50_000, 3_000_000, jump=0.15)
        if c.get("registered") and c.get("total") and c["registered"][-1][1] > c["total"][-1][1]:
            p.append("COMEX registered above total")
    elif name == "etf":
        p += series_issues("GLD", (d.get("etf") or {}).get("GLD"), 200_000, 2_500_000, jump=0.05)
    elif name == "etf2":
        for k, ser in (d.get("etf2") or {}).items():
            if ser:
                p += series_issues(k, ser, 1_000, 2_500_000, jump=0.10)
    elif name == "physical":
        ph = d.get("physical") or {}
        if ph.get("efp"):
            p += series_issues("EFP", [r[:2] for r in ph["efp"]], -300, 300, jump_abs=150, max_gap=10)
        if ph.get("sh"):
            p += series_issues("Shanghai premium", [r[:2] for r in ph["sh"]], -400, 400, jump_abs=200, max_gap=14)
    elif name in ("cot", "cot_legacy"):
        recs = (d.get("cot_disagg") or {}).get("mm") if name == "cot" else d.get("noncomm")
        p += series_issues(name, [r[:2] for r in recs or []], 0, 2_000_000, tail=12)
        if recs and min(recs[-1][1], recs[-1][2]) < 0:
            p.append(f"{name}: negative positions")
    elif name == "imf":
        p += series_issues("central banks", (d.get("central_banks") or {}).get("series"), 10_000_000, 50_000_000, jump=0.03, max_gap=45, tail=12)
    elif name == "macro":
        m = d.get("macro") or {}
        p += series_issues("real 10y", m.get("real10"), -4, 8, jump_abs=0.6, max_gap=10)
        for k, lo, hi, ja in (("nom10", -1, 15, 0.6), ("be10", -1, 6, 0.5)):
            if m.get(k):
                p += series_issues(k, m[k], lo, hi, jump_abs=ja, max_gap=10)
        if m.get("dxy"):
            p += series_issues("DXY", m["dxy"], 60, 160, jump=0.04, max_gap=10)
        if m.get("vix"):
            p += series_issues("VIX", m["vix"], 5, 150, max_gap=10)
        if m.get("nq"):
            p += series_issues("NQ", m["nq"], 1000, 100000, jump=0.15, max_gap=10)
        f = m.get("fed") or {}
        if f:
            if not -1 <= (f.get("effr") or [0, 0])[1] <= 20:
                p.append(f"Fed funds {f.get('effr')} out of range")
            for k in ("cpi", "core"):
                if f.get(k) and not -5 <= f[k]["yoy"] <= 25:
                    p.append(f"{k} YoY {f[k]['yoy']} out of range")
    elif name == "oi_daily":
        p += series_issues("CME daily OI", [r[:2] for r in d.get("oi_daily") or []], 100_000, 2_000_000, jump=0.15, max_gap=7)
    elif name == "gex":
        g = d.get("gex") or {}
        S = g.get("spot") or 0
        if not 50 <= S <= 2000:
            p.append(f"GEX: GLD price {S} out of range")
        for k in ("call_wall", "put_wall", "zero_all"):
            if g.get(k) is not None and S and abs(g[k] / S - 1) > 0.3:
                p.append(f"GEX: {k} {g[k]} too far from price {S}")
    return p


# ---------------------------------------------------------------- bias track record (scored after 14 days)
OUTLOOK_DAYS, OUTLOOK_BAND = 14, 0.02


def score_outlooks():
    """يقارن كل انحياز واحتمالاته بما فعله الذهب بعد 14 يومًا: صعود إن زاد أكثر من 2٪، هبوط إن نقص أكثر من 2٪، وإلا عرضي.
    Brier = مجموع (الاحتمال − النتيجة)² على الاتجاهات الثلاثة: 0 مثالي، و0.667 لمن يعطي كل اتجاه الثلث."""
    log_ = load(OUTLOOK_LOG, [])
    daily = load(PRICES_OUT, {}).get("daily") or []
    if not log_ or not daily:
        return None
    days = [r[0] for r in daily]
    changed = False
    for e in log_:
        if e.get("result") or not e.get("base"):
            continue
        due = (dt.date.fromisoformat(e["base"]["date"]) + dt.timedelta(days=OUTLOOK_DAYS)).isoformat()
        if days[-1] <= due:
            continue                                    # لم تُغلق جلسة بعد تاريخ الاستحقاق
        i = bisect.bisect_right(days, due) - 1
        close = daily[i][1]
        ret = close / e["base"]["price"] - 1
        out = "up" if ret > OUTLOOK_BAND else "down" if ret < -OUTLOOK_BAND else "flat"
        probs = dict(zip(e["dirs"], [x / 100 for x in e["p"]]))
        brier = sum((probs.get(k, 0) - (k == out)) ** 2 for k in ("up", "flat", "down"))
        e["result"] = {"date": daily[i][0], "price": close, "ret": round(ret * 100, 2), "outcome": out,
                       "bias_hit": {"bullish": "up", "bearish": "down", "neutral": "flat"}[e["bias"]] == out,
                       "p_outcome": round(probs.get(out, 0) * 100), "brier": round(brier, 3)}
        if e.get("base_rates"):
            tot = sum(e["base_rates"].values()) or 1
            e["result"]["brier_base"] = round(sum((e["base_rates"][k] / tot - (k == out)) ** 2 for k in ("up", "flat", "down")), 3)
        changed = True
    if changed:
        save(OUTLOOK_LOG, log_)
    done = [e["result"] for e in log_ if e.get("result")]
    summ = {"n": len(done), "pending": len(log_) - len(done)}
    if done:
        summ.update(hit=round(sum(r["bias_hit"] for r in done) / len(done) * 100),
                    brier=round(sum(r["brier"] for r in done) / len(done), 3), brier_naive=0.667,
                    p_outcome=round(sum(r["p_outcome"] for r in done) / len(done)))
    based = [r for r in done if "brier_base" in r]
    if based:
        b, bb = sum(r["brier"] for r in based) / len(based), sum(r["brier_base"] for r in based) / len(based)
        summ.update(brier_base=round(bb, 3), skill=round((1 - b / bb) * 100, 1) if bb else None, n_base=len(based))
    dr = outlook_direction(log_)
    if dr:
        summ["direction"] = dr
    return {"summary": summ, "days": OUTLOOK_DAYS, "band": OUTLOOK_BAND * 100, "rows": log_[-30:]}


def outlook_direction(log_):
    """الاتجاه وحده: في النوافذ التي تحرّك فيها الذهب أكثر من 2٪ فقط، هل كان ميل النموذج (صعود مقابل هبوط، مع تجاهل
    العرضي) في الجهة الصحيحة؟ ومعه Brier ثنائي لحصة الصعود صعود ÷ (صعود + هبوط): 0.25 لمن يقول 50/50 دائمًا."""
    dec = [e for e in log_ if e.get("result") and e["result"]["outcome"] != "flat"]
    if not dec:
        return None
    hits, br, brb = [], [], []
    for e in dec:
        P = dict(zip(e["dirs"], e["p"]))
        u, dn = P.get("up", 0), P.get("down", 0)
        up = e["result"]["outcome"] == "up"
        if u != dn:
            hits.append((u > dn) == up)
        br.append(((u / (u + dn) if u + dn else 0.5) - up) ** 2)
        b = e.get("base_rates") or {}
        if b.get("up", 0) + b.get("down", 0):
            brb.append((b["up"] / (b["up"] + b["down"]) - up) ** 2)
    out = {"n": len(dec), "n_lean": len(hits), "hit": round(sum(hits) / len(hits) * 100) if hits else None,
           "brier": round(sum(br) / len(br), 3), "brier_coin": 0.25,
           "windows": _windows([e["base"]["date"] for e in dec if e.get("base")])}
    if len(brb) == len(br):
        bb = sum(brb) / len(brb)
        out.update(brier_base=round(bb, 3), skill=round((1 - out["brier"] / bb) * 100, 1) if bb else None)
    return out


MIRROR_MIN = 10


def performance_mirror():
    """مرآة الأداء: ملخص سجل انحيازات النموذج نفسه بعد تقييمها، يُعطى له ليصحح معايرته. يعمل تلقائيًا من التقييم
    العاشر؛ قبل ذلك العيّنة أصغر من أن تفيد وقد تضلّل."""
    log_ = [e for e in load(OUTLOOK_LOG, []) if e.get("result")]
    if len(log_) < MIRROR_MIN:
        return None
    n = len(log_)
    res = [e["result"] for e in log_]
    calib = {}
    for k in ("up", "flat", "down"):
        pred = [dict(zip(e["dirs"], e["p"])).get(k, 0) for e in log_]
        calib[k] = {"avg_predicted_pct": round(sum(pred) / n), "realized_pct": round(sum(r["outcome"] == k for r in res) / n * 100)}
    starts = sorted(dt.date.fromisoformat(e["base"]["date"]) for e in log_)
    indep, last = 0, None
    for d in starts:                                     # نوافذ 14 يومًا لا تتداخل: العيّنة المستقلة الفعلية
        if last is None or (d - last).days >= OUTLOOK_DAYS:
            indep, last = indep + 1, d
    out = {"scored_reports": n, "independent_14d_windows": indep,
           "bias_hit_rate_pct": round(sum(r["bias_hit"] for r in res) / n * 100),
           "avg_brier": round(sum(r["brier"] for r in res) / n, 3),
           "calibration": calib,
           "last_10": [{"date": e["date"], "bias": e["bias"], "p": dict(zip(e["dirs"], e["p"])), "outcome": e["result"]["outcome"],
                        "ret_pct": e["result"]["ret"]} for e in log_[-10:]],
           "note": ("Your own scored forecasts (14 days, ±2% band). Use them to correct systematic errors, e.g. if you predict "
                    "a direction more often than it happens, lower it. Overlapping windows make the sample smaller than it looks "
                    "(see independent_14d_windows): do not over-react to a few results.")}
    based = [r for r in res if "brier_base" in r]
    if based:
        b, bb = sum(r["brier"] for r in based) / len(based), sum(r["brier_base"] for r in based) / len(based)
        out["vs_base_rates"] = {"avg_brier_base_rate_forecast": round(bb, 3), "skill_pct": round((1 - b / bb) * 100, 1) if bb else None,
                                "n": len(based)}
    return out


# ---------------------------------------------------------------- main: release-aware scheduler
SECTIONS = {
    "price": (fetch_prices, ()),
    "imf": (fetch_imf, ("countries", "central_banks")),
    "cot": (fetch_cot, ("banks", "cot_disagg")),
    "cot_legacy": (fetch_cot_legacy, ("noncomm", "cot_extra", "oi_fo")),
    "deliveries": (fetch_deliveries, ()),
    "comex": (fetch_comex, ()),
    "etf": (fetch_gld, ("etf",)),
    "etf2": (fetch_etfs, ("etf2", "etf2_errors")),
    "physical": (fetch_physical, ("physical",)),
    "f13": (fetch_f13, ("f13",)),
    "wgc": (fetch_wgc, ("wgc_etf",)),
    "macro": (fetch_macro, ("macro", "liquidation")),
    "gex": (fetch_gex, ("gex",)),
    "oi_daily": (fetch_oi_daily, ("oi_daily",)),
}
CHECK_KEYS = {"comex": ("comex",), "deliveries": ("deliveries",)}     # مفاتيح تُبنى من ملفات السجل لا من دالة الجلب
MAJOR = ("cot", "cot_legacy", "imf", "f13", "wgc")      # تقارير كبرى تستحق تقريرًا ذكيًا جديدًا


def latest_of(name, d):
    """تاريخ أحدث تقرير لكل مصدر، لاكتشاف صدور تقرير جديد."""
    try:
        if name == "imf":
            return max(s[-1][0] for s in (d.get("countries") or {}).values())
        if name == "cot":
            return d["cot_disagg"]["swap"][-1][0]
        if name == "cot_legacy":
            return d["noncomm"][-1][0]
        if name == "deliveries":
            return max((d.get("deliveries") or {}).get("daily") or {"": 0}) or None
        if name == "comex":
            return d["comex"]["total"][-1][0]
        if name == "etf":
            return d["etf"]["GLD"][-1][0]
        if name == "etf2":
            return max(v[-1][0] for v in d["etf2"].values() if v)
        if name == "physical":
            return max(v[-1][0] for k, v in d["physical"].items() if k in ("efp", "sh") and v)
        if name == "f13":
            return d["f13"]["periods"][-1]["p"]
        if name == "wgc":
            return d["wgc_etf"][-1]["data_month"]
        if name == "macro":
            return d["macro"]["real10"][-1][0]
        if name == "gex":
            return d["gex"]["asof"][:10]
        if name == "oi_daily":
            return d["oi_daily"][-1][0]
    except Exception:
        return None
    return None


def hours_since(iso):
    if not iso:
        return 1e9
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 3600


def is_due(name, st, prev):
    """متى نفحص كل مصدر: قرب موعد التقرير المتوقع فقط، بدل فحص كل شيء كل ساعة."""
    if os.environ.get("FULL_REFRESH", "").strip().lower() in ("1", "true", "yes"):
        return True
    now = dt.datetime.now(dt.timezone.utc)
    if name == "price":
        return True                                                        # كل ساعة
    if name in ("cot", "cot_legacy"):
        last = latest_of(name, prev)
        if not last:
            return True
        # بيانات الثلاثاء تصدر الجمعة 15:30 بتوقيت نيويورك (19:30 أو 20:30 UTC). نبدأ الفحص من 19:00 UTC الجمعة
        # ونستمر كل ساعة حتى يظهر التقرير (يتأخر أحيانًا إلى الاثنين بسبب العطل الرسمية).
        expected = dt.datetime.fromisoformat(last).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=10, hours=19)
        return now >= expected
    if name in ("deliveries", "comex", "etf", "etf2", "physical", "macro", "gex", "oi_daily"):
        return now.weekday() != 6 and hours_since(st.get("checked")) >= 2.8      # تقارير يومية: كل 3 ساعات
    if name in ("imf", "f13", "wgc"):
        return hours_since(st.get("checked")) >= 11.5                          # شهرية وربعية: مرتين يوميًا
    return True


def main():
    prev = load(OUT, {})
    state = load(STATE, {})
    releases = load(RELEASES, {})
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    data = {k: v for k, v in prev.items() if k not in ("generated_utc", "sources")}
    status = {"checked_utc": now, "sources": {}}
    new_major = {}
    for name, (fn, keys) in SECTIONS.items():
        st = state.setdefault(name, {})
        if not is_due(name, st, prev):
            status["sources"][name] = {**st, "ok": st.get("ok", True), "skipped": True}
            continue
        before = latest_of(name, prev)
        st["checked"] = now
        try:
            data.update(fn() or {})
            if name == "comex":
                data["comex"] = load(COMEX_HIST, {"registered": [], "eligible": [], "total": []})
            if name == "deliveries":
                dh = load(DELIV_HIST, {"daily": {}, "mtd": {}})
                data["deliveries"] = {"daily": {d: dh["daily"][d] for d in sorted(dh["daily"])[-120:]}, "mtd": dh.get("mtd", {})}
            problems = check_data(name, data)
            if problems:
                raise RuntimeError("data check: " + "; ".join(problems)[:260])
            st.update(ok=True, last_ok=now, error=None)
            after = latest_of(name, data)
            if after and after != before:
                st["new"] = now
                st["latest"] = after
                log(f"{name}: NEW report {after} (previous {before})")
                if before is not None:                              # أول تشغيل لا يعرف لحظة النشر الحقيقية
                    releases.setdefault(name, {})[after] = now      # اللحظة التي رُصد فيها التقرير الجديد
                    if name in MAJOR:
                        new_major[name] = after
        except Exception as e:
            log(name, "FAILED:", repr(e)[:300])
            st.update(ok=False, error=(str(e) or repr(e))[:300])
            for key in keys + CHECK_KEYS.get(name, ()):
                if key in prev:
                    data[key] = prev[key]
        status["sources"][name] = {**st}
    for k in releases:
        releases[k] = dict(sorted(releases[k].items())[-80:])
    data["releases"] = releases
    try:
        pos = positioning(data)
        if pos:
            prev_note = (prev.get("positioning") or {}).get("ai_note")
            try:
                note = positioning_note(pos, prev_note)
            except Exception as e:
                log("positioning note FAILED:", repr(e)[:200])
                note = prev_note
            if note:
                pos["ai_note"] = note
            data["positioning"] = pos
    except Exception as e:
        log("positioning FAILED:", repr(e)[:200])
    try:
        data["direction_filter"] = direction_filter(data)
    except Exception as e:
        log("direction filter FAILED:", repr(e)[:200])
    pending = state.get("ai_pending") or {}
    new_major = {**pending, **new_major}
    disc = state.setdefault("ai_discovery", {})
    if "left" in disc:                                # الصيغة القديمة (عدد ثابت من التقارير)
        disc["reports"] = 4 - disc.pop("left")
        disc.pop("last", None)
    now_d = dt.datetime.now(dt.timezone.utc)
    today_s = now_d.date().isoformat()
    if not disc.get("done") and today_s > DISCOVERY_END:
        disc["done"] = today_s
        log("AI discovery: ended (deadline reached without a new COT report)")
    active = not disc.get("done")
    last_ai = ((prev.get("ai") or {}).get("latest") or {}).get("date")
    forced = os.environ.get("AI_FORCE", "").strip().lower() in ("1", "true", "yes")
    if active and not new_major and (forced or (now_d.weekday() < 4 and now_d.hour >= DISCOVERY_HOUR_UTC
                                                and last_ai != today_s)):
        new_major = {"discovery": today_s}
    elif active and new_major:
        new_major = {**new_major, "discovery": now_d.date().isoformat()}   # كل تقرير في المرحلة يبحث بحرية ويُسجَّل
    vn = now_d.astimezone(VIENNA)
    last_utc = ((prev.get("ai") or {}).get("latest") or {}).get("generated_utc")
    last_vn = dt.datetime.fromisoformat(last_utc).astimezone(VIENNA).date() if last_utc else None
    due_daily = vn.weekday() < 5 and (vn.hour, vn.minute) >= DAILY_REPORT_AT and last_vn != vn.date()
    if due_daily and not active:
        new_major = {**new_major, "daily": vn.date().isoformat()}
    fail = state.get("ai_fail") or {}
    wait_until = fail.get("retry_after")
    try:
        if not active and not forced and not due_daily:
            if new_major:
                log("AI report: held for the next daily report (13:30 Vienna):", ", ".join(new_major))
            raise _AIWait()
        if new_major and wait_until and now_d.isoformat() < wait_until:
            # بعد فشل طلب مكلف لا نعيده كل نصف ساعة: ننتظر AI_RETRY_HOURS ثم نحاول مرة أخرى
            log(f"AI report: last attempt failed; next try after {wait_until}")
            raise _AIWait()
        ai, wrote = ai_report(data, prev, new_major, releases)
        state.pop("ai_fail", None)
        if wrote and active:
            disc["reports"] = disc.get("reports", 0) + 1
            if ("cot" in new_major or "cot_legacy" in new_major) and today_s >= DISCOVERY_COT_FROM:
                disc["done"] = today_s
                log(f"AI discovery: ended after the COT report ({disc['reports']} reports logged)")
            else:
                log(f"AI discovery: {disc['reports']} report(s) logged; continues until the next COT report")
        ai = _no_cost(ai)
        if ai:
            data["ai"] = ai
        state["ai_pending"] = {}
        status["sources"]["ai"] = {"ok": True, "checked": now, "wrote": wrote, "off": not bool(ai)}
    except _AIWait:
        state["ai_pending"] = new_major
        if prev.get("ai"):
            data["ai"] = _no_cost(prev["ai"])
    except Exception as e:
        log("ai FAILED:", repr(e)[:300])
        state["ai_pending"] = new_major                             # نعيد المحاولة بعد مهلة، لا في كل تشغيل
        state["ai_fail"] = {"at": now_d.isoformat(), "error": repr(e)[:200],
                            "retry_after": (now_d + dt.timedelta(hours=AI_RETRY_HOURS)).isoformat()}
        status["sources"]["ai"] = {"ok": False, "checked": now, "error": str(e)[:300]}
        if prev.get("ai"):
            data["ai"] = _no_cost(prev["ai"])
    if data.get("direction_filter"):
        try:
            data["direction_filter"]["tv_code"], data["direction_filter"]["focus"] = tv_code(data["direction_filter"], data.get("ai"))
        except Exception as e:
            log("tv code FAILED:", repr(e)[:200])
    try:
        track = score_outlooks()
        if track:
            data["outlook_track"] = track
    except Exception as e:
        log("outlook scoring FAILED:", repr(e)[:200])
    try:
        save(SCORECARD, scorecard(data))
    except Exception as e:
        log("scorecard FAILED:", repr(e)[:200])
    # data.json لا يُكتب إلا إذا تغيّرت البيانات فعلًا، كي لا يتضخم سجل المستودع
    core = lambda x: json.dumps({k: v for k, v in x.items() if k not in ("generated_utc", "sources", "data_utc")},
                                ensure_ascii=False, sort_keys=True)
    if core(data) != core(prev):
        data["data_utc"] = now
        save(OUT, data)
        log("data.json updated")
    else:
        log("data.json unchanged")
    status["data_utc"] = data.get("data_utc") or prev.get("data_utc") or prev.get("generated_utc")
    save(STATUS_OUT, status)
    save(STATE, state)
    save(RELEASES, releases)
    ran = [n for n, x in status["sources"].items() if not x.get("skipped")]
    log(f"done. checked: {ran}; skipped (not due): {[n for n, x in status['sources'].items() if x.get('skipped')]}")


if __name__ == "__main__":
    main()
