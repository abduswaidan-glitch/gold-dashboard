"""مستمع FinancialJuice: يبقى متصلًا طوال التشغيل الحي ويكتب كل رسالة (عنوان أو بيان اقتصادي) سطرًا في ملف مؤقت
خارج المستودع (FJ_BUFFER)، فيقرؤه fetch_gold.py في كل تحديث. لا يُرفع شيء من نص العناوين إلى المستودع العام.
الخطة المجانية: الرسائل تصل بعد 10 دقائق من نشرها، واتصال واحد فقط، ولا إعادة لما فات أثناء الانقطاع."""
import asyncio
import json
import os
import random
import sys
import time

import websockets

URL = os.environ.get("FJ_URL") or "wss://stream.financialjuice.com/v1/stream?takeover=true"   # takeover: يحرّر مقعد اتصال قديم عالق بدل الرفض
STOP = {4001: "bad or missing key", 4003: "IP/origin not allowed", 4030: "replaced by a newer connection"}


def log(*a):
    print("[fj]", *a, flush=True)


async def run(key, path):
    attempt, counts = 0, {}
    while True:
        try:
            async with websockets.connect(URL, additional_headers={"X-API-Key": key},
                                          ping_interval=None, open_timeout=30) as ws:
                attempt = 0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    t = msg.get("type")
                    if t == "ping":
                        continue
                    if t == "hello":
                        log(f"connected; channels={msg.get('channels')} delay={msg.get('delay_seconds')}s")
                        continue
                    if t == "error":
                        log("server error:", msg.get("code"), msg.get("message"))
                        continue
                    msg["recv_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    counts[t] = counts.get(t, 0) + 1
                    if sum(counts.values()) % 25 == 1:
                        log("received so far:", counts)
        except websockets.ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            if code in STOP:
                log(f"rejected ({code}: {STOP[code]}); listener stops")
                return
            log("connection closed:", code)
        except Exception as e:
            log("connection error:", repr(e)[:160])
        attempt += 1
        await asyncio.sleep(min(30, 2 ** min(attempt, 5)) + random.random())


def main():
    key = os.environ.get("FJ_API_KEY", "").strip()
    path = os.environ.get("FJ_BUFFER", "").strip()
    if not key or not path:
        log("no FJ_API_KEY or FJ_BUFFER: listener not started")
        return 0
    asyncio.run(run(key, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
