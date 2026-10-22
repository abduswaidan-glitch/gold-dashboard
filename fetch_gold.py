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
    return {"noncomm": recs}


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
def yahoo_chart(symbol, rng, interval, period=None):
    errors = []
    span = f"period1={period[0]}&period2={period[1]}" if period else f"range={rng}"
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?{span}&interval={interval}"
        try:
            res = browser_get(url, headers={"Accept": "application/json"}).json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]["close"]
            return res.get("meta", {}), [(t, float(c)) for t, c in zip(res["timestamp"], q) if c]
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
    d = dict(load(PRICES_OUT, {}).get("daily") or [])
    meta, recent = yahoo_chart("GC=F", "1mo", "1d")
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
           "last": last, "daily": sorted([k, v] for k, v in d.items()), "hourly": h[-150:]}
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
MACRO_FRED = {"real10": "DFII10", "nom10": "DGS10", "be10": "T10YIE", "vix": "VIXCLS"}
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
            full[key] = fred_series(sid, "2008-01-01" if key in ("real10", "vix") else MACRO_SHOW)
        except Exception as e:
            errors.append(f"{sid}: {str(e)[:90]}")
    for key, sym, start in (("dxy", "DX-Y.NYB", dt.date(2018, 1, 1)), ("nq", "NQ=F", dt.date(2008, 1, 1))):
        try:
            full[key] = sorted([k, round(v, 3)] for k, v in yahoo_daily(sym, start).items())
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:90]}")
    if "real10" not in full:
        raise RuntimeError("macro: " + " | ".join(errors))
    out = {k: [r for r in v if r[0] >= MACRO_SHOW] for k, v in full.items()}
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
 "headline_en":"one sentence","headline_ar":"جملة واحدة بالعربية الفصحى","headline_it":"una frase in italiano",
 "headline_es":"una frase en español"}
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
        for i in range(0, len(todo), 120):
            batch = todo[i:i + 120]
            resp = _claude({"model": WGC_MODEL, "max_tokens": 4000, "system": F13_TYPE_SYSTEM,
                            "messages": [{"role": "user", "content": "\n".join(f"{c}|{names[c]}" for c in batch)}]}, key)
            txt = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            got = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            types.update({c: (got.get(c) if got.get(c) in F13_KINDS else "other") for c in batch if c in got})
        save(F13_TYPES, types)
        log(f"13F institution types: classified {len(todo)} new institutions")
    return {c: types[c] for c in names if c in types}


# ---------------------------------------------------------------- AI daily report (Claude API)
AI_HIST = os.path.join("data", "ai_history.json")
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5-5")
AI_URL = "https://api.anthropic.com/v1/messages"
AI_LANGS = ("ar", "en", "it", "es")
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
- Use web search (up to 4 searches) only for today's gold-relevant events: Federal Reserve and rates, US dollar and yields,
  geopolitics, central-bank gold news, China and India demand, major ETF or bank news. Cite each event's source URL.
  Real and nominal yields, breakeven inflation, the dollar index and GLD gamma levels are in the platform data: take those
  numbers from there, not from search results.
- Connect the sections: say when they agree or contradict each other (e.g. speculators crowded long while ETFs see outflows).
- When gold has fallen in recent sessions, judge whether the drop looks like forced selling to meet margin calls elsewhere
  (gold falling together with equities, VIX jumping, real yields not rising, the dollar bid) or a fundamental repricing
  (real yields or the dollar driving it). Use the forced_selling_monitor and its historical base rates, plus physical demand
  (Shanghai premium, EFP, ETF flows, central banks), and state how confident you are. A base rate is not a forecast.
- Be factual. This is not investment advice: no buy/sell recommendations, no price targets.
- After weighing every section and today's events, give your own analytical bias for gold over the next 14 calendar days
  and exactly three scenarios, one per direction, with estimated probabilities in whole percent that sum to 100, most likely
  first. The directions are scored objectively later against the COMEX gold close 14 days from now:
  "up" = more than 2% above today's price, "down" = more than 2% below, "flat" = within ±2%. Calibrate: your track record
  is public. Each scenario gets one short line on why, grounded in the data or events.
- Write the same report in four languages, with the same content and numbers in each:
  Arabic in correct Modern Standard Arabic (فصحى); clear, professional English; natural, correct professional Italian;
  natural, correct professional Spanish (Spain). Keep technical terms in English in every language (COT, EFP, ETF, Long, Short).
Return ONLY one JSON object, no markdown fences, with exactly this shape:
{"outlook": {"bias": "bullish|bearish|neutral", "scenarios": [{"dir": "up|flat|down", "p": 45}, {"dir": "...", "p": 30}, {"dir": "...", "p": 25}]},
 "ar": {"headline": "...", "tone": "إيجابي|سلبي|محايد|متباين", "summary": ["...", "..."],
        "sections": [{"title": "...", "body": "..."}], "events": [{"title": "...", "impact": "...", "source": "https://..."}],
        "watch": ["..."], "risks": "...",
        "outlook": {"bias_note": "one line on why", "scenarios": [{"title": "...", "why": "..."}, {...}, {...}]}},
 "en": {same keys, "tone": "positive|negative|neutral|mixed"},
 "it": {same keys, "tone": "positivo|negativo|neutrale|misto"},
 "es": {same keys, "tone": "positivo|negativo|neutral|mixto"}}
The three scenarios in every language follow the order of outlook.scenarios; each direction appears exactly once.
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


def _cost(model, u):
    pin, pout = next(((i, o) for pre, i, o in PRICES_PER_MTOK if str(model).startswith(pre)), (10, 50))
    return (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6 \
        + ((u.get("server_tool_use") or {}).get("web_search_requests", 0)) * 0.01


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
        if lang in ("ar", "en") and not (isinstance(sc, list) and len(sc) >= 3):
            return None
        if isinstance(rep.get(lang), dict) and isinstance(sc, list):
            rep[lang]["outlook"] = {"bias_note": str(lo.get("bias_note") or ""),
                                    "scenarios": [{"title": str(x.get("title") or ""), "why": str(x.get("why") or "")}
                                                  for x in sc[:3] if isinstance(x, dict)]}
    return {"bias": o["bias"], "p": p, **({"dirs": dirs} if dirs else {})}


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
                                                        for k, v in (new_major or {}).items()}
    digest = json.dumps(digest_obj, ensure_ascii=False, separators=(",", ":"))
    user = (f"Date (UTC): {today}.\nPlatform data (JSON; t = tonnes, contracts = COMEX GC 100-oz contracts):\n{digest}\n\n"
            "Search for today's key gold-related events, then return the JSON report.")
    messages = [{"role": "user", "content": user}]
    sources, text, resp = [], "", None
    ws_new = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
    ws_old = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
    # fallbacks "default": إن رفض النموذج الطلب يُعاد تشغيله على نموذج بديل من Anthropic تلقائيًا (ويُعرض اسمه الحقيقي)
    for tools, fallback in ((ws_new, True), (ws_old, True), (ws_new, False), (None, False)):
        try:
            payload = {"model": AI_MODEL, "max_tokens": 32000, "system": AI_SYSTEM, "messages": list(messages),
                       "output_config": {"effort": "high"}}      # Opus 5.5 يبدأ افتراضيًا بـ medium
            if tools:
                payload["tools"] = tools
            if fallback:
                payload["fallbacks"] = "default"
            beta = "server-side-fallback-2026-07-01" if fallback else None
            resp = _claude(payload, key, beta, timeout=900)
            for _ in range(3):                       # البحث الطويل قد يتوقف مؤقتًا (pause_turn) فنكمله
                if resp.get("stop_reason") != "pause_turn":
                    break
                payload["messages"] = payload["messages"] + [{"role": "assistant", "content": resp["content"]}]
                resp = _claude(payload, key, beta, timeout=900)
            break
        except ClaudeHTTPError as e:
            if not 400 <= e.status < 500 or e.status == 429:
                raise                                # خطأ خادم أو ضغط: نعيد المحاولة في التشغيل التالي بدل تكرار طلب مكلف
            log("AI request rejected" + (" (with web search)" if tools else "") + (" (with fallbacks)" if fallback else "") + ":", str(e)[:200])
            resp = None
    if not resp:
        raise RuntimeError("Claude API: all attempts failed")
    if resp.get("stop_reason") == "refusal":
        raise RuntimeError(f"Claude API: request declined ({(resp.get('stop_details') or {}).get('category')})")
    for b in resp.get("content", []):
        if b.get("type") == "text":
            text += b.get("text", "")
        elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            for it in b["content"]:
                u = it.get("url", "")
                if u.startswith("https://") and u not in [x["url"] for x in sources]:
                    sources.append({"url": u, "title": (it.get("title") or "")[:140]})
    i, j = text.find("{"), text.rfind("}")
    try:
        rep = json.loads(text[i:j + 1])
        if not (isinstance(rep.get("ar"), dict) and isinstance(rep.get("en"), dict)):
            raise ValueError("missing ar/en")
    except Exception as e:
        raise RuntimeError(f"AI report: could not parse JSON ({e}); first chars: {text[:120]!r}")
    outlook = _outlook(rep)
    if not outlook:
        log("AI report: bias/scenarios missing or malformed; report kept without them")
    u = resp.get("usage") or {}
    model = resp.get("model", AI_MODEL)
    cost = _cost(model, u)
    langs = {k: rep[k] for k in AI_LANGS if isinstance(rep.get(k), dict)}  # it/es اختياريتان: الواجهة تعرض الإنجليزية إن غابتا
    latest = {"date": today, "generated_utc": now.replace(microsecond=0).isoformat(), "model": model,
              **langs, "outlook": outlook, "sources": sources[:10],
              "usage": {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                        "searches": (u.get("server_tool_use") or {}).get("web_search_requests", 0), "est_cost_usd": round(cost, 4)}}
    last_price = load(PRICES_OUT, {}).get("last") or {}
    if outlook and outlook.get("dirs") and last_price.get("price"):
        olog = load(OUTLOOK_LOG, [])
        olog.append({"id": latest["generated_utc"], "date": today, "model": model, "bias": outlook["bias"], "p": outlook["p"],
                     "dirs": outlook["dirs"], "base": {"date": str(last_price.get("time") or today)[:10], "price": last_price["price"]}})
        save(OUTLOOK_LOG, olog[-300:])
    hist = load(AI_HIST, [])
    hist = [h for h in hist if h.get("generated_utc") != latest["generated_utc"]] + [latest]
    hist = hist[-30:]
    save(AI_HIST, hist)
    log(f"AI report ok: {today}, model {latest['model']}, {u.get('input_tokens')} in / {u.get('output_tokens')} out tokens, "
        f"{latest['usage']['searches']} searches, est. ${cost:.3f}")
    latest["trigger"] = list((new_major or {}).keys()) or ["manual"]
    hist[-1]["trigger"] = latest["trigger"]
    save(AI_HIST, hist)
    return {"latest": latest, "history": [{"date": h["date"], **{k: h[k] for k in AI_LANGS if k in h}, "outlook": h.get("outlook"),
                                           "sources": h.get("sources", []),
                                           "model": h.get("model"), "generated_utc": h.get("generated_utc"),
                                           "trigger": h.get("trigger", [])} for h in hist[-14:]]}, True


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
        changed = True
    if changed:
        save(OUTLOOK_LOG, log_)
    done = [e["result"] for e in log_ if e.get("result")]
    summ = {"n": len(done), "pending": len(log_) - len(done)}
    if done:
        summ.update(hit=round(sum(r["bias_hit"] for r in done) / len(done) * 100),
                    brier=round(sum(r["brier"] for r in done) / len(done), 3), brier_naive=0.667,
                    p_outcome=round(sum(r["p_outcome"] for r in done) / len(done)))
    return {"summary": summ, "days": OUTLOOK_DAYS, "band": OUTLOOK_BAND * 100, "rows": log_[-30:]}


# ---------------------------------------------------------------- main: release-aware scheduler
SECTIONS = {
    "price": (fetch_prices, ()),
    "imf": (fetch_imf, ("countries", "central_banks")),
    "cot": (fetch_cot, ("banks", "cot_disagg")),
    "cot_legacy": (fetch_cot_legacy, ("noncomm",)),
    "deliveries": (fetch_deliveries, ()),
    "comex": (fetch_comex, ()),
    "etf": (fetch_gld, ("etf",)),
    "etf2": (fetch_etfs, ("etf2", "etf2_errors")),
    "physical": (fetch_physical, ("physical",)),
    "f13": (fetch_f13, ("f13",)),
    "wgc": (fetch_wgc, ("wgc_etf",)),
    "macro": (fetch_macro, ("macro", "liquidation")),
    "gex": (fetch_gex, ("gex",)),
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
    if name in ("deliveries", "comex", "etf", "etf2", "physical", "macro", "gex"):
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
    pending = state.get("ai_pending") or {}
    new_major = {**pending, **new_major}
    try:
        ai, wrote = ai_report(data, prev, new_major, releases)
        if ai:
            data["ai"] = ai
        state["ai_pending"] = {}
        status["sources"]["ai"] = {"ok": True, "checked": now, "wrote": wrote, "off": not bool(ai)}
    except Exception as e:
        log("ai FAILED:", repr(e)[:300])
        state["ai_pending"] = new_major                             # نعيد المحاولة في التشغيل التالي
        status["sources"]["ai"] = {"ok": False, "checked": now, "error": str(e)[:300]}
        if prev.get("ai"):
            data["ai"] = prev["ai"]
    try:
        track = score_outlooks()
        if track:
            data["outlook_track"] = track
    except Exception as e:
        log("outlook scoring FAILED:", repr(e)[:200])
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
