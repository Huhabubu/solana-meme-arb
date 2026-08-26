#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 首买事件研究 V3：真实逐笔建仓 + 双源价格路径。

V3 在 V2 基础上增加：
- 所有研究事件都抓 T0-5s ~ T0+300s 的全市场逐笔窗口；
- 完整 1s K 线存在时，主路径仍使用 K 线 high/low/close；
- 1s K 线不完整但逐笔窗口完整时，使用真实逐笔成交路径回退；
- 对有 K 线的事件同时计算 trade-path，用于验证逐笔回退与 K 线口径的差异；
- 固定时点的逐笔 mark 使用目标时刻之前 5 秒内最后一笔成交，更接近 close 语义；
- MFE/MAE 使用建仓后至 T0+300s 的最高/最低真实逐笔成交价。

仍然是历史毛收益研究；建仓价是市场第一笔参考成交，不是指定下单金额的可执行 VWAP。
"""

from __future__ import annotations

import importlib.util
import math
import statistics
import threading
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

# V2 会分别抓 entry 小窗口和 300s 大窗口。缓存底层窗口，避免 V3 为逐笔路径再次请求。
_original_fetch_market_window = base.fetch_market_window
_window_cache: Dict[Tuple[str, str, int, int, int], Tuple[List[Dict[str, Any]], bool, int]] = {}
_cache_lock = threading.Lock()


def cached_fetch_market_window(
    chain_id: str,
    mint: str,
    start_ms: int,
    end_ms: int,
    max_pages: int,
):
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


def last_trade_before(
    rows: List[Dict[str, Any]],
    target_ms: int,
    lookback_ms: int = 5000,
) -> Optional[Dict[str, Any]]:
    """目标时刻之前最近一笔真实成交，最多向前看 lookback_ms。"""
    start = target_ms - lookback_ms
    best: Optional[Dict[str, Any]] = None
    best_ts = -1
    for r in rows:
        ts = base.inum(r.get("timestamp"))
        if ts < start:
            continue
        if ts > target_ms:
            break
        if ts >= best_ts and base.row_price(r):
            best = r
            best_ts = ts
    return best


def trade_excursion(
    rows: List[Dict[str, Any]],
    entry_price: float,
    entry_ts: int,
    t0: int,
    end_ms: int,
) -> Dict[str, Optional[float]]:
    future = [
        r for r in rows
        if entry_ts <= base.inum(r.get("timestamp")) <= end_ms and base.row_price(r)
    ]
    if not future:
        return {
            "mfe_bps": None, "mae_bps": None,
            "mfe_after_t0_s": None, "mfe_after_entry_s": None,
            "mae_after_t0_s": None, "mae_after_entry_s": None,
            "mfe_price": None, "mae_price": None,
        }
    max_row = max(future, key=lambda r: float(base.row_price(r) or 0))
    min_row = min(future, key=lambda r: float(base.row_price(r) or math.inf))
    max_p = float(base.row_price(max_row))
    min_p = float(base.row_price(min_row))
    max_ts = base.inum(max_row.get("timestamp"))
    min_ts = base.inum(min_row.get("timestamp"))
    mfe = base.bps(entry_price, max_p)
    mae = base.bps(entry_price, min_p)
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


def trade_path_for_delay(
    event: Dict[str, Any],
    market: List[Dict[str, Any]],
    delay: int,
) -> Dict[str, Optional[float]]:
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
        mark_row = last_trade_before(market, target, lookback_ms=5000)
        mark = base.row_price(mark_row)
        ret = base.bps(float(entry_price), mark)
        out[f"to_{horizon}s_bps"] = round(ret, 3) if ret is not None else None
        out[f"mark_{horizon}s_trade_ts"] = (
            float(base.inum(mark_row.get("timestamp"))) if mark_row else None
        )

    ex = trade_excursion(market, float(entry_price), entry_ts, t0, end_ms)
    out.update(ex)
    ret300 = out.get("to_300s_bps")
    mfe = ex.get("mfe_bps")
    out["profit_giveback_to_300s_bps"] = (
        round(float(mfe) - float(ret300), 3)
        if mfe is not None and ret300 is not None else None
    )
    out["mfe_retained_at_300s_ratio"] = (
        round(float(ret300) / float(mfe), 4)
        if mfe is not None and float(mfe) > 0 and ret300 is not None else None
    )
    return out


def analyze_event(
    kol: Dict[str, Any],
    pnl_item: Dict[str, Any],
    history: List[Dict[str, Any]],
    history_complete: bool,
    history_pages: int,
    args,
) -> Optional[Dict[str, Any]]:
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

    event["path_source"] = (
        "kline_1s" if event.get("candle_window_complete")
        else "market_trades" if market_complete
        else None
    )
    event["trade_path_available"] = bool(market_complete)

    # 无论 K 线是否完整，只要逐笔完整，都计算一份 parallel trade-path 供交叉验证。
    if market_complete:
        for delay in DELAYS_S:
            tp = trade_path_for_delay(event, market, delay)
            for name, value in tp.items():
                event[f"tradepath_entry_{delay}s_{name}"] = value

            # K 线缺失时，主指标回退为真实逐笔路径。
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
        "market_trades": sum(e.get("path_source") == "market_trades" for e in events),
        "none": sum(e.get("path_source") is None for e in events),
    }

    overlap = [
        e for e in events
        if e.get("candle_window_complete") and e.get("trade_path_available")
        and e.get("entry_1s_price") is not None
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


# process_candidate/main 在 V2 内通过模块全局名称查找 analyze_event / summarize，直接替换即可。
v2.analyze_event = analyze_event
v2.summarize = summarize

if __name__ == "__main__":
    v2.main()
