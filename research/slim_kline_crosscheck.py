#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
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

rows, complete = base.fetch_candles(CHAIN_ID, MINT, START, END)
print("rows", len(rows), "complete", complete)
for r in rows:
    rel = (int(r["timestamp"]) - T0) / 1000
    print(f"{rel:+.0f}s O={r['open']:.12f} H={r['high']:.12f} L={r['low']:.12f} C={r['close']:.12f}")

pre = [r for r in rows if T0 - 5_000 <= int(r["timestamp"]) < T0]
post5 = [r for r in rows if T0 <= int(r["timestamp"]) <= T0 + 5_000]
post10 = [r for r in rows if T0 <= int(r["timestamp"]) <= T0 + 10_000]
post20 = [r for r in rows if T0 <= int(r["timestamp"]) <= T0 + 20_000]

def bps(a: float, b: float) -> float:
    return (b / a - 1.0) * 10000.0

def low(xs):
    return min((float(r["low"]) for r in xs), default=None)

def high(xs):
    return max((float(r["high"]) for r in xs), default=None)

def close_before():
    xs = [r for r in rows if int(r["timestamp"]) < T0]
    return float(xs[-1]["close"]) if xs else None

metrics = {"complete": complete, "row_count": len(rows), "pre5_high": high(pre), "pre_last_close": close_before()}
for name, xs in (("post5", post5), ("post10", post10), ("post20", post20)):
    lo = low(xs)
    metrics[name + "_low"] = lo
    if lo and metrics["pre5_high"]:
        metrics[name + "_from_pre5_high_bps"] = bps(metrics["pre5_high"], lo)
    if lo and metrics["pre_last_close"]:
        metrics[name + "_from_preclose_bps"] = bps(metrics["pre_last_close"], lo)

print("METRICS", json.dumps(metrics, ensure_ascii=False, indent=2))
