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

IMF_URL = "https://api.imf.org/external/sdmx/2.1/data/IRFCL/.IRFCLDT1_IRFCL56V_FTO..M"
COT_URLS = [
    "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",   # Disaggregated, Futures only
    "https://publicreporting.cftc.gov/resource/kh3c-gbw2.json",   # Disaggregated, Futures + Options
]
GOLD_CODE = "088691"
CME_URL = "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls"
GLD_PAGE = "https://www.spdrgoldshares.com/usa/historical-data/"
GLD_PAGES = [GLD_PAGE, "https://www.spdrgoldshares.com/usa/"]
GLD_CANDIDATES = [
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
def find_key(row, must, avoid=("change", "pct", "traders", "conc", "old", "other")):
    keys = [k for k in row
            if all(m in k.lower() for m in must) and not any(a in k.lower() for a in avoid)]
    keys.sort(key=len)
    return keys[0] if keys else None


def fetch_cot():
    params = {
        "$where": f"cftc_contract_market_code='{GOLD_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "170",
    }
    rows, errors = [], []
    for url in COT_URLS:
        try:
            rows = get(url, params=params, headers={"Accept": "application/json"}).json()
            if rows:
                break
            errors.append(f"{url}: empty")
        except Exception as e:
            errors.append(f"{url}: {e}")
            log("COT", url, e)
    if not rows:
        raise RuntimeError("COT: no data — " + (errors[-1] if errors else "")[:200])

    sample = rows[0]
    k = {
        "sl": find_key(sample, ("swap", "positions", "long")),
        "ss": find_key(sample, ("swap", "positions", "short")),
        "pl": find_key(sample, ("prod_merc", "positions", "long")),
        "ps": find_key(sample, ("prod_merc", "positions", "short")),
    }
    if not all(k.values()):
        raise RuntimeError(f"COT: fields not found {k}")

    to_kg = CONTRACT_OZ * OZ_KG
    swap, prod = [], []
    latest = None
    for r in rows:
        d = str(r.get("report_date_as_yyyy_mm_dd", ""))[:10]
        vals = {n: num(r.get(f)) for n, f in k.items()}
        if not d or any(v is None for v in vals.values()):
            continue
        swap.append([d, round((vals["sl"] - vals["ss"]) * to_kg)])
        prod.append([d, round((vals["pl"] - vals["ps"]) * to_kg)])
        if latest is None or d > latest["date"]:
            latest = {"date": d,
                      "swap_long": round(vals["sl"] * to_kg), "swap_short": round(vals["ss"] * to_kg),
                      "prod_long": round(vals["pl"] * to_kg), "prod_short": round(vals["ps"] * to_kg)}
    swap.sort()
    prod.sort()
    if not swap:
        raise RuntimeError("COT: parsed 0 rows")
    log(f"COT ok: {len(swap)} weeks, latest {swap[-1][0]}")
    return {"banks": {"swap_net": swap, "prod_net": prod, "latest": latest}}


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

    vals = {}
    for i in range(sh.nrows):
        t = re.sub(r"\s+", " ", row_text(i)).strip()
        nums = [c for c in sh.row_values(i) if isinstance(c, float)]
        if not nums:
            continue
        if "TOTAL REGISTERED" in t:
            vals["registered"] = nums[-1]
        elif "TOTAL ELIGIBLE" in t:
            vals["eligible"] = nums[-1]
        elif "COMBINED TOTAL" in t:
            vals["total"] = nums[-1]
    if "registered" not in vals or "eligible" not in vals:
        raise RuntimeError(f"CME: totals not found in sheet (found {list(vals)})")
    vals.setdefault("total", vals["registered"] + vals["eligible"])

    hist = load(COMEX_HIST, {"registered": [], "eligible": [], "total": []})
    for name in ("registered", "eligible", "total"):
        upsert(hist.setdefault(name, []), report_date, round(vals[name] * OZ_KG))
    save(COMEX_HIST, hist)
    log(f"COMEX ok: {report_date}, total {vals['total']:,.0f} oz")


# ---------------------------------------------------------------- SPDR GLD
def table_rows(content):
    """يحوّل ملف XLSX أو CSV إلى صفوف."""
    head = content[:8]
    if head.startswith(b"%PDF"):
        raise RuntimeError("file is a PDF, not data")
    if head.startswith(b"PK"):                       # xlsx
        openpyxl = ensure("openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if len(rows) > 10:
                break
        return rows
    text = content.decode("utf-8-sig", errors="replace")
    return list(csv.reader(text.splitlines()))


def cell_day(v):
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return parse_day(v)


def parse_gld_rows(rows):
    def low(r):
        return [str(c or "").strip().lower() for c in r]
    hi = next((i for i, r in enumerate(rows[:80])
               if any("date" in c for c in low(r)) and any(("tonnes" in c or "ounces" in c) for c in low(r))), None)
    if hi is None:
        raise RuntimeError("header not found; first row: " + str(rows[0][:4] if rows else "empty")[:100])
    header = low(rows[hi])
    di = next(i for i, h in enumerate(header) if "date" in h)
    tcols = [i for i, h in enumerate(header) if "tonnes" in h]
    ocols = [i for i, h in enumerate(header) if "ounces" in h]
    if tcols:
        ci, factor = next((i for i in tcols if "trust" in header[i]), tcols[0]), 1000.0
    else:
        ci, factor = next((i for i in ocols if "trust" in header[i]), ocols[0]), OZ_KG
    out = {}
    for r in rows[hi + 1:]:
        if len(r) <= max(di, ci):
            continue
        d, v = cell_day(r[di]), num(r[ci])
        if d and v and d >= START_DAY:
            out[d] = round(v * factor)
    if not out:
        raise RuntimeError(f"parsed 0 rows (columns: {header[:5]})")
    return sorted([d, v] for d, v in out.items())


def find_archive_links(page_url, html):
    """يلتقط روابط ملفات xlsx/csv من الصفحة، بما فيها الروابط المخبّأة داخل JSON."""
    from urllib.parse import urljoin
    text = html.decode("utf-8", "replace").replace("\\/", "/").replace("\\u002F", "/")
    found = []
    for m in re.finditer(r"""(?:https?:)?//[^\s"'<>()]+?\.(?:xlsx|csv)(?:\?[^\s"'<>()]*)?"""
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
    for u in urls[:8]:
        try:
            series = parse_gld_rows(table_rows(browser_get(u, headers={"Referer": GLD_PAGE}).content))
            log(f"GLD ok: {len(series)} days, latest {series[-1][0]} ({u})")
            return {"etf": {"GLD": series}}
        except Exception as e:
            errors.append(f"{u.rsplit('/', 1)[-1][:50]}: {e}")
    raise RuntimeError("GLD: " + " | ".join(errors)[:260])


# ---------------------------------------------------------------- main
SECTIONS = {
    "imf": (fetch_imf, ("countries", "central_banks")),
    "cot": (fetch_cot, ("banks",)),
    "comex": (fetch_comex, ()),
    "etf": (fetch_gld, ("etf",)),
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
    save(OUT, data)
    ok = [n for n, s in data["sources"].items() if s["ok"]]
    log(f"done. ok: {ok}")


if __name__ == "__main__":
    main()
