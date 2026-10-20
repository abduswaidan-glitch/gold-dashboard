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

OUT = "data.json"
COMEX_HIST = os.path.join("data", "comex_history.json")
DELIV_HIST = os.path.join("data", "deliveries_history.json")
F13_HIST = os.path.join("data", "f13_history.json")
GLD_OZPS = os.path.join("data", "gld_ozps.json")
ETF_HIST = os.path.join("data", "etf_history.json")
SGE_HIST = os.path.join("data", "sge_history.json")
GLD_LBMA = os.path.join("data", "gld_lbma_pm.json")

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
    rows = socrata_gold(COT_URLS)
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
    rows = socrata_gold(LEGACY_URLS, limit=170)
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
    gi = next((i for i, h in enumerate(header) if "nav per gld in gold" in h or ("per" in h and "in gold" in h)), None)
    ni = next((i for i, h in enumerate(header) if ("nav/share" in h or "nav per share" in h) and "gold" not in h), None)
    out, ozps, lbma = {}, {}, {}
    for r in rows[hi + 1:]:
        if len(r) <= max(di, ci):
            continue
        d, v = cell_day(r[di]), num(r[ci])
        if d and gi is not None and len(r) > gi:
            g = num(r[gi])
            if g and 50 < g < 110 and d >= "2015-01-01":
                ozps[d[:7]] = round(g / 1000, 6)        # آخر قيمة في كل شهر
                nav = num(r[ni]) if ni is not None and len(r) > ni else None
                if nav and d >= "2023-01-01":
                    lbma[d] = round(nav / (g / 1000), 2)  # سعر LBMA PM الذي يُقيَّم به GLD
        if d and v and d >= START_DAY:
            out[d] = round(v * factor)
    if not out:
        raise RuntimeError(f"parsed 0 rows (columns: {header[:5]})")
    if ozps and save_ozps:
        save(GLD_OZPS, ozps)
    if lbma and save_ozps:
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
            return {dt.datetime.utcfromtimestamp(t).date().isoformat(): float(c) for t, c in zip(ts, cl) if c}
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
                d = dt.datetime.utcfromtimestamp(ts / 1000 + 8 * 3600).date().isoformat()
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
    return {"f13": out}


# ---------------------------------------------------------------- main
SECTIONS = {
    "imf": (fetch_imf, ("countries", "central_banks")),
    "cot": (fetch_cot, ("banks", "cot_disagg")),
    "cot_legacy": (fetch_cot_legacy, ("noncomm",)),
    "deliveries": (fetch_deliveries, ()),
    "comex": (fetch_comex, ()),
    "etf": (fetch_gld, ("etf",)),
    "etf2": (fetch_etfs, ("etf2", "etf2_errors")),
    "physical": (fetch_physical, ("physical",)),
    "f13": (fetch_f13, ("f13",)),
}


def main():
    prev = load(OUT, {})
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    data = {"generated_utc": now, "sources": {}}
    for name, (fn, keys) in SECTIONS.items():
        prev_src = prev.get("sources", {}).get(name, {})
        try:
            result = fn() or {}
            data.update(result)
            data["sources"][name] = {"ok": True, "last_ok": now}
        except Exception as e:
            log(name, "FAILED:", repr(e))
            data["sources"][name] = {"ok": False, "error": (str(e) or repr(e))[:300],
                                     "last_ok": prev_src.get("last_ok")}
            for key in keys:
                if key in prev:
                    data[key] = prev[key]
    data["comex"] = load(COMEX_HIST, {"registered": [], "eligible": [], "total": []})
    dh = load(DELIV_HIST, {"daily": {}, "mtd": {}})
    data["deliveries"] = {"daily": {d: dh["daily"][d] for d in sorted(dh["daily"])[-120:]}, "mtd": dh.get("mtd", {})}
    save(OUT, data)
    ok = [n for n, s in data["sources"].items() if s["ok"]]
    log(f"done. ok: {ok}")


if __name__ == "__main__":
    main()
