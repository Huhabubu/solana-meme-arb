#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 首买事件研究 V3：真实逐笔建仓 + 双源价格路径。

V3 在 V2 基础上增加：
- 所有研究事件都抓 T0-5s ~ T0+300s 的全市场逐笔窗口；
- 完整 1s K 线存在时，主路径仍使用 K 线 high/low/close；
- 1s K 线不完整但逐笔窗口完整时，用真实逐笔成交按秒重建 OHLC 路径；
- 对孤立、相对同秒/邻近秒基准偏离 >=100x 的异常成交做审计式过滤；
- 对有 K 线的事件同时计算 trade-path，用于验证逐笔回退与 K 线口径差异；
- 固定时点取目标时刻之前 5 秒内最后一根重建秒线 close；
- MFE/MAE 使用建仓后至 T0+300s 的重建秒线 high/low。

仍然是历史毛收益研究；建仓价是市场第一笔参考成交，不是指定下单金额的可执行 VWAP。
"""

from __future__ import annotations

import importlib.util
import math
import statistics
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

V2_PATH = Path(__file__).with_name("KOL首买事件研究_v2.py")
spec = importlib.util.spec_from_file_location("kol_first_buy_v2", V2_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load V2 module: {V2_PATH}")
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)

base = v2.base
DELAYS_S = v2.DELAYS_S
HORIZONS_S = v2.HORIZONS_S

_original_fetch_market_window = base.fetch_market_window
_window_cache: Dict[Tuple[str, str, int, int, int], Tuple[List[Dict[str, Any]], bool, int]] = {}
_cache_lock = threading.Lock()


def cached_fetch_market_window(chain_id: str, mint: str, start_ms: int, end_ms: int, max_pages: int):
    key = (str(chain_id), str(mint), int(start_ms), int(end_ms), int(max_pages))
    with _cache_lock:
        hit = _window_cache.get(key)
    if hit is not None:
        return hit
    value = _original_fetch_market_window(chain_id, mint, start_ms, end_ms, max_pages)
    with _cache_lock:
        _window_cache[key] = value
    return value


base.fetch_market_window = cached_fetch_market_window
_original_analyze_event = v2.analyze_event
_original_summarize = v2.summarize


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def build_trade_bars(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, float]], int, int]:
    """从逐笔成交重建 1 秒 OHLC，并过滤极端孤立 print。

    规则尽量保守：
    - 同一秒 >=3 笔时，以同秒中位数为基准；
    - 否则以相邻 +/-2 秒的秒级中位数为基准；
    - 仅当单笔价格相对基准 >=100x 或 <=1/100x 时剔除。
    因此一整秒、多笔共同发生的真实大涨不会因绝对涨幅大而被删除。
    """
    priced: List[Tuple[int, float, Dict[str, Any]]] = []
    by_sec_prices: Dict[int, List[float]] = defaultdict(list)
    for r in rows:
        p = base.row_price(r)
        ts = base.inum(r.get("timestamp"))
        if p and ts:
            sec = ts // 1000
            priced.append((sec, float(p), r))
            by_sec_prices[sec].append(float(p))

    sec_med = {sec: statistics.median(ps) for sec, ps in by_sec_prices.items() if ps}
    kept: List[Tuple[int, float, Dict[str, Any]]] = []
    removed = 0
    for sec, p, r in priced:
        same = by_sec_prices.get(sec, [])
        if len(same) >= 3:
            baseline = sec_med.get(sec)
        else:
            neighbors = [sec_med[s] for s in range(sec - 2, sec + 3) if s != sec and s in sec_med]
            baseline = statistics.median(neighbors) if neighbors else sec_med.get(sec)
        if baseline and baseline > 0:
            ratio = p / baseline
            if ratio >= 100.0 or ratio <= 0.01:
                removed += 1
                continue
        kept.append((sec, p, r))

    grouped: Dict[int, List[Tuple[float, Dict[str, Any]]]] = defaultdict(list)
    for sec, p, r in kept:
        grouped[sec].append((p, r))

    bars: List[Dict[str, float]] = []
    for sec in sorted(grouped):
        xs = sorted(grouped[sec], key=lambda pr: base.inum(pr[1].get("timestamp")))
        prices = [p for p, _ in xs]
        if not prices:
            continue
        bars.append({
            "timestamp": float(sec * 1000),
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "trade_count": float(len(prices)),
        })
    return bars, len(priced), removed


def last_bar_before(bars: List[Dict[str, float]], target_ms: int, lookback_ms: int = 5000) -> Optional[Dict[str, float]]:
    start = target_ms - lookback_ms
    best: Optional[Dict[str, float]] = None
    for b in bars:
        ts = int(b["timestamp"])
        if ts < start:
            continue
        if ts > target_ms:
            break
        best = b
    return best


def bar_excursion(
    bars: List[Dict[str, float]], entry_price: float, entry_ts: int, t0: int, end_ms: int
) -> Dict[str, Optional[float]]:
    future = [b for b in bars if entry_ts <= int(b["timestamp"]) <= end_ms]
    if not future:
        return {
            "mfe_bps": None, "mae_bps": None,
            "mfe_after_t0_s": None, "mfe_after_entry_s": None,
            "mae_after_t0_s": None, "mae_after_entry_s": None,
            "mfe_price": None, "mae_price": None,
        }
    max_bar = max(future, key=lambda b: float(b["high"]))
    min_bar = min(future, key=lambda b: float(b["low"]))
    max_p, min_p = float(max_bar["high"]), float(min_bar["low"])
    max_ts, min_ts = int(max_bar["timestamp"]), int(min_bar["timestamp"])
    mfe, mae = base.bps(entry_price, max_p), base.bps(entry_price, min_p)
    return {
        "mfe_bps": round(mfe, 3) if mfe is not None else None,
        "mae_bps": round(mae, 3) if mae is not None else None,
        "mfe_after_t0_s": round((max_ts - t0) / 1000, 3),
        "mfe_after_entry_s": round((max_ts - entry_ts) / 1000, 3),
        "mae_after_t0_s": round((min_ts - t0) / 1000, 3),
        "mae_after_entry_s": round((min_ts - entry_ts) / 1000, 3),
        "mfe_price": max_p,
        "mae_price": min_p,
    }


def trade_path_for_delay(event: Dict[str, Any], bars: List[Dict[str, float]], delay: int) -> Dict[str, Optional[float]]:
    t0 = base.inum(event.get("t0_ms"))
    end_ms = t0 + 300_000
    entry_price = event.get(f"entry_{delay}s_price")
    entry_ts = base.inum(event.get(f"entry_{delay}s_trade_ts"))
    out: Dict[str, Optional[float]] = {}
    if not entry_price or not entry_ts:
        return out

    for horizon in HORIZONS_S:
        if horizon <= delay:
            continue
        target = t0 + horizon * 1000
        mark_bar = last_bar_before(bars, target, lookback_ms=5000)
        mark = float(mark_bar["close"]) if mark_bar else None
        ret = base.bps(float(entry_price), mark)
        out[f"to_{horizon}s_bps"] = round(ret, 3) if ret is not None else None
        out[f"mark_{horizon}s_bar_ts"] = float(int(mark_bar["timestamp"])) if mark_bar else None

    ex = bar_excursion(bars, float(entry_price), entry_ts, t0, end_ms)
    out.update(ex)
    ret300, mfe = out.get("to_300s_bps"), ex.get("mfe_bps")
    out["profit_giveback_to_300s_bps"] = (
        round(float(mfe) - float(ret300), 3) if mfe is not None and ret300 is not None else None
    )
    out["mfe_retained_at_300s_ratio"] = (
        round(float(ret300) / float(mfe), 4)
        if mfe is not None and float(mfe) > 0 and ret300 is not None else None
    )
    return out


def analyze_event(kol, pnl_item, history, history_complete, history_pages, args):
    event = _original_analyze_event(kol, pnl_item, history, history_complete, history_pages, args)
    if not event:
        return None
    event["analysis_version"] = 3
    t0 = base.inum(event.get("t0_ms"))
    start = t0 - args.pre_window_seconds * 1000
    end = t0 + args.post_window_seconds * 1000
    key = (str(event["chain_id"]), str(event["mint"]), start, end, int(args.max_market_pages))
    with _cache_lock:
        cached = _window_cache.get(key)
    market, market_complete, _ = cached if cached is not None else ([], False, 0)

    bars, raw_price_points, removed_points = build_trade_bars(market) if market_complete else ([], 0, 0)
    event["trade_path_raw_price_points"] = raw_price_points
    event["trade_path_outlier_points_removed"] = removed_points
    event["trade_path_bar_count"] = len(bars)
    event["trade_path_available"] = bool(market_complete and bars)
    event["path_source"] = (
        "kline_1s" if event.get("candle_window_complete")
        else "market_trades_1s_ohlc" if event["trade_path_available"]
        else None
    )

    if event["trade_path_available"]:
        for delay in DELAYS_S:
            tp = trade_path_for_delay(event, bars, delay)
            for name, value in tp.items():
                event[f"tradepath_entry_{delay}s_{name}"] = value

            if not event.get("candle_window_complete") and event.get(f"entry_{delay}s_price") is not None:
                for horizon in HORIZONS_S:
                    if horizon <= delay:
                        continue
                    event[f"entry_{delay}s_to_{horizon}s_bps"] = tp.get(f"to_{horizon}s_bps")
                for suffix in (
                    "mfe_bps", "mae_bps", "mfe_after_t0_s", "mfe_after_entry_s",
                    "mae_after_t0_s", "mae_after_entry_s", "mfe_price", "mae_price",
                    "profit_giveback_to_300s_bps", "mfe_retained_at_300s_ratio",
                ):
                    event[f"entry_{delay}s_{suffix}"] = tp.get(suffix)

    event["entry_1s_valid"] = event.get("entry_1s_price") is not None
    event["analysis_valid"] = bool(event["entry_1s_valid"] and event.get("path_source"))
    return event


def _med(xs: List[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _original_summarize(events)
    out["analysis_version"] = 3
    out["path_source_counts"] = {
        "kline_1s": sum(e.get("path_source") == "kline_1s" for e in events),
        "market_trades_1s_ohlc": sum(e.get("path_source") == "market_trades_1s_ohlc" for e in events),
        "none": sum(e.get("path_source") is None for e in events),
    }
    out["trade_path_outliers"] = {
        "events_with_removed_points": sum(base.inum(e.get("trade_path_outlier_points_removed")) > 0 for e in events),
        "removed_points_total": sum(base.inum(e.get("trade_path_outlier_points_removed")) for e in events),
    }

    overlap = [
        e for e in events
        if e.get("candle_window_complete") and e.get("trade_path_available") and e.get("entry_1s_price") is not None
    ]
    validation: Dict[str, Any] = {"n": len(overlap)}
    for suffix in ("to_10s_bps", "to_30s_bps", "to_300s_bps", "mfe_bps", "mae_bps", "mfe_after_entry_s"):
        diffs = []
        for e in overlap:
            a = e.get(f"entry_1s_{suffix}")
            b = e.get(f"tradepath_entry_1s_{suffix}")
            if a is not None and b is not None:
                diffs.append(float(b) - float(a))
        validation[f"trade_minus_kline_{suffix}_median"] = _med(diffs)
        validation[f"trade_minus_kline_{suffix}_n"] = len(diffs)
    out["trade_path_validation"] = validation
    return out


v2.analyze_event = analyze_event
v2.summarize = summarize

if __name__ == "__main__":
    print("V3_ACTIVE 3")
    v2.main()
