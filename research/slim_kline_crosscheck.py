#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import time
import urllib.parse
from pathlib import Path

BASE_PATH = Path(__file__).parent / "kol_trading_behavior" / "KOL首买事件研究.py"
spec = importlib.util.spec_from_file_location("kol_base", BASE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load KOL base module")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

CHAIN_ID = "501"
MINT = "xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
T0 = 1788278910000  # 2026-09-02 00:08:30 UTC+8, OKX trigger timestamp
START = T0 - 10_000
END = T0 + 20_000


def bps(a: float, b: float) -> float:
    return (b / a - 1.0) * 10000.0


def fetch_bar(bar: str, after_ms: int, limit: int = 30):
    params = {
        "chainId": CHAIN_ID,
        "address": MINT,
        "after": str(after_ms),
        "bar": bar,
        "limit": str(limit),
        "t": str(int(time.time() * 1000)),
    }
    status, body = base.request_json(
        base.KLINE_URL + "?" + urllib.parse.urlencode(params),
        referer=f"https://web3.okx.com/zh-hans/token/{CHAIN_ID}/{MINT}",
    )
    out = []
    for item in body.get("data") or []:
        if isinstance(item, list) and len(item) >= 5:
            out.append({
                "timestamp": base.inum(item[0]),
                "open": base.fnum(item[1]),
                "high": base.fnum(item[2]),
                "low": base.fnum(item[3]),
                "close": base.fnum(item[4]),
            })
    out.sort(key=lambda r: r["timestamp"])
    return status, out

rows, complete = base.fetch_candles(CHAIN_ID, MINT, START, END)
print("=== 1s ===")
print("rows", len(rows), "complete", complete)
for r in rows:
    rel = (int(r["timestamp"]) - T0) / 1000
    print(f"{rel:+.0f}s O={r['open']:.12f} H={r['high']:.12f} L={r['low']:.12f} C={r['close']:.12f}")
if rows:
    event = min(rows, key=lambda r: abs(int(r["timestamp"]) - T0))
    print("1s_event_open_to_low_bps", bps(event["open"], event["low"]))
    print("1s_event_high_to_low_bps", bps(event["high"], event["low"]))
    print("1s_event_open_to_close_bps", bps(event["open"], event["close"]))

print("=== 1m ===")
status, mins = fetch_bar("1m", T0 + 180_000, 20)
print("status", status, "rows", len(mins))
near = [r for r in mins if T0 - 180_000 <= r["timestamp"] <= T0 + 180_000]
for r in near:
    rel = (r["timestamp"] - T0) / 1000
    print(f"{rel:+.0f}s O={r['open']:.12f} H={r['high']:.12f} L={r['low']:.12f} C={r['close']:.12f} open_low_bps={bps(r['open'], r['low']):.3f} high_low_bps={bps(r['high'], r['low']):.3f}")

print("=== all-market event prints from prior event-study reference ===")
print("trigger avg execution price ~0.004605 USDC/SLIM")
print("known event-second OKX prints include ~0.004577 buys and ~0.004618-0.004625 sells")
