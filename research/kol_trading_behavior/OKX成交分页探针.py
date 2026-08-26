#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：验证 OKX trading-history/filter-list 的大页与时间定位参数。"""

import json
import time
import urllib.request

URL = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
CHAIN_ID = "56"
MINT = "0xb93ee31356bd947fc935f6cc68dab4dd77c37777"  # 阿峰 EASY 历史样本
TARGET_MS = 1787659759000  # 2026-08-25 20:09:19 +08:00


def post(payload):
    req = urllib.request.Request(
        URL + "?t=" + str(int(time.time() * 1000)),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://web3.okx.com/zh-hans/market/dex",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body, time.perf_counter() - t0


def base(limit=30):
    return {
        "desc": True,
        "orderBy": "timestamp",
        "limit": limit,
        "tradingHistoryFilter": {
            "chainId": CHAIN_ID,
            "tokenContractAddress": MINT,
            "type": "0",
            "currentUserWalletAddress": "",
            "userAddressList": [],
            "volumeMin": "", "volumeMax": "",
            "priceMin": "", "priceMax": "",
            "amountMin": "", "amountMax": "",
        },
    }


def summarize(name, payload):
    try:
        body, elapsed = post(payload)
        data = body.get("data") or {}
        rows = data.get("list") or []
        ts = [int(r.get("timestamp") or 0) for r in rows]
        result = {
            "name": name,
            "code": body.get("code"),
            "rows": len(rows),
            "elapsed_s": round(elapsed, 4),
            "hasMore": data.get("hasMore"),
            "newest_ts": max(ts) if ts else None,
            "oldest_ts": min(ts) if ts else None,
            "distance_oldest_to_target_s": round((min(ts) - TARGET_MS) / 1000, 3) if ts else None,
            "first_id": rows[0].get("id") if rows else None,
            "last_id": rows[-1].get("id") if rows else None,
        }
        print("PROBE", json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print("PROBE", json.dumps({"name": name, "error": repr(exc)}, ensure_ascii=False))


# 1) 单页大小
for lim in (30, 100, 500, 1000):
    summarize(f"limit_{lim}", base(lim))

# 2) synthetic dataId / cursor
for cursor in (
    str(TARGET_MS),
    f"{TARGET_MS}!@#0!@#0",
    f"{TARGET_MS}!@#999!@#999999999999",
):
    p = base(30)
    p["dataId"] = cursor
    summarize("dataId=" + cursor, p)

# 3) tradingHistoryFilter 内候选时间字段
candidates = [
    ("startTime/endTime", {"startTime": TARGET_MS - 5000, "endTime": TARGET_MS + 300000}),
    ("startTimestamp/endTimestamp", {"startTimestamp": TARGET_MS - 5000, "endTimestamp": TARGET_MS + 300000}),
    ("timestampMin/timestampMax", {"timestampMin": TARGET_MS - 5000, "timestampMax": TARGET_MS + 300000}),
    ("timeMin/timeMax", {"timeMin": TARGET_MS - 5000, "timeMax": TARGET_MS + 300000}),
    ("begin/end", {"begin": TARGET_MS - 5000, "end": TARGET_MS + 300000}),
]
for name, extra in candidates:
    p = base(30)
    p["tradingHistoryFilter"].update(extra)
    summarize("filter_" + name, p)

# 4) 顶层候选时间字段
for name, extra in candidates:
    p = base(30)
    p.update(extra)
    summarize("top_" + name, p)
