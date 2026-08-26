#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计阿峰老样本中的极端 MFE，不自动删除数据。

对 MFE > 1000% (100,000 bps) 的事件记录：
- 主路径来源（1s Kline / trade OHLC）
- Kline 与 trade-path 的 MFE 是否互相确认
- trade-path 最大价所在秒的成交笔数
- 最大价前后 5 秒逐秒 OHLC

目的：区分真实持续暴涨与孤立异常 print，再决定是否需要更严格过滤。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

OLD_PATH = Path(__file__).with_name("KOL阿峰老样本验证.py")
spec = importlib.util.spec_from_file_location("afeng_old_validation_for_audit", OLD_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load old validation: {OLD_PATH}")
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)

v2 = old.v2
v4 = old.v4
v3 = v4.v3
base = old.base
_old_summarize = old.summarize
THRESHOLD_BPS = 100_000.0


def _round(v, n=6):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None


def audit_event(e: Dict[str, Any], args) -> Dict[str, Any]:
    t0 = base.inum(e.get("t0_ms"))
    entry_ts = base.inum(e.get("entry_1s_trade_ts"))
    mfe_ts = t0 + int(float(e.get("entry_1s_mfe_after_t0_s") or 0) * 1000)
    start = t0 - args.pre_window_seconds * 1000
    end = t0 + args.post_window_seconds * 1000
    key = (str(e["chain_id"]), str(e["mint"]), start, end, int(args.max_market_pages))
    with v3._cache_lock:
        cached = v3._window_cache.get(key)
    market, complete, pages = cached if cached is not None else ([], False, 0)
    bars, raw_points, removed = v3.build_trade_bars(market) if complete else ([], 0, 0)

    # 找 trade-path 的最大 high 及邻域。
    future = [b for b in bars if entry_ts <= int(b["timestamp"]) <= end]
    max_bar = max(future, key=lambda b: float(b["high"])) if future else None
    neighbor_bars: List[Dict[str, Any]] = []
    if max_bar:
        msec = int(max_bar["timestamp"]) // 1000
        for b in bars:
            sec = int(b["timestamp"]) // 1000
            if msec - 5 <= sec <= msec + 5:
                neighbor_bars.append({
                    "offset_from_max_s": sec - msec,
                    "timestamp": int(b["timestamp"]),
                    "open": _round(b["open"]),
                    "high": _round(b["high"]),
                    "low": _round(b["low"]),
                    "close": _round(b["close"]),
                    "trade_count": int(b["trade_count"]),
                })

    main_mfe = e.get("entry_1s_mfe_bps")
    trade_mfe = e.get("tradepath_entry_1s_mfe_bps")
    kline_and_trade = bool(e.get("candle_window_complete") and e.get("trade_path_available"))
    dual_confirm = False
    if kline_and_trade and main_mfe is not None and trade_mfe is not None:
        a, b = float(main_mfe), float(trade_mfe)
        denom = max(abs(a), abs(b), 1.0)
        dual_confirm = abs(a - b) / denom <= 0.10

    # trade-path 持续性：最大价秒有 >=2 笔，或者邻近秒 close 至少达到最大 high 的 20%。
    persistent_trade_move = False
    if max_bar:
        max_high = float(max_bar["high"])
        max_count = int(max_bar["trade_count"])
        adjacent = [x for x in neighbor_bars if x["offset_from_max_s"] != 0 and x.get("close")]
        adjacent_support = any(float(x["close"]) >= max_high * 0.20 for x in adjacent)
        persistent_trade_move = max_count >= 2 or adjacent_support

    if dual_confirm:
        classification = "dual_source_confirmed"
    elif e.get("path_source") == "market_trades_1s_ohlc" and persistent_trade_move:
        classification = "trade_path_persistent"
    elif e.get("path_source") == "kline_1s" and not kline_and_trade:
        classification = "kline_only_needs_review"
    else:
        classification = "suspect_or_single_source"

    return {
        "kol": e.get("kol"),
        "symbol": e.get("symbol"),
        "mint": e.get("mint"),
        "t0_ms": t0,
        "event_age_hours": e.get("event_age_hours"),
        "entry_1s_price": e.get("entry_1s_price"),
        "main_path_source": e.get("path_source"),
        "main_mfe_bps": main_mfe,
        "main_mfe_after_t0_s": e.get("entry_1s_mfe_after_t0_s"),
        "trade_mfe_bps": trade_mfe,
        "trade_mfe_after_t0_s": e.get("tradepath_entry_1s_mfe_after_t0_s"),
        "candle_window_complete": bool(e.get("candle_window_complete")),
        "trade_path_available": bool(e.get("trade_path_available")),
        "dual_source_confirmed_within_10pct": dual_confirm,
        "market_window_complete": bool(complete),
        "market_pages": pages,
        "trade_raw_price_points": raw_points,
        "trade_outliers_removed": removed,
        "trade_max_bar": {
            "timestamp": int(max_bar["timestamp"]) if max_bar else None,
            "high": _round(max_bar["high"]) if max_bar else None,
            "low": _round(max_bar["low"]) if max_bar else None,
            "close": _round(max_bar["close"]) if max_bar else None,
            "trade_count": int(max_bar["trade_count"]) if max_bar else None,
        },
        "trade_neighbor_bars": neighbor_bars,
        "persistent_trade_move": persistent_trade_move,
        "audit_classification": classification,
    }


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _old_summarize(events)
    # args 由 main 放在模块全局 audit_args。
    extreme = [
        e for e in events
        if e.get("entry_1s_mfe_bps") is not None
        and float(e["entry_1s_mfe_bps"]) >= THRESHOLD_BPS
    ]
    audits = [audit_event(e, audit_args) for e in extreme]
    out["extreme_path_audit"] = {
        "threshold_bps": THRESHOLD_BPS,
        "event_count": len(audits),
        "classification_counts": {
            name: sum(a["audit_classification"] == name for a in audits)
            for name in sorted({a["audit_classification"] for a in audits})
        },
        "events": audits,
    }
    print("EXTREME_AUDIT " + json.dumps(out["extreme_path_audit"], ensure_ascii=False))
    return out


v2.summarize = summarize

if __name__ == "__main__":
    # base main 会 parse args；这里先复用 parser 的结果需要最小侵入：包装 parse_args。
    original_parse = base.parse_args

    def capture_parse_args():
        global audit_args
        audit_args = original_parse()
        return audit_args

    base.parse_args = capture_parse_args
    print("AFENG_EXTREME_PATH_AUDIT_ACTIVE 1")
    v2.main()
