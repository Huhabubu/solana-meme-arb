#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 首买事件研究 V4：决策时点对齐 + 简单退出规则。

目标：消除 V3 中“用 0~5s 跟单压力筛 +1s 入场”的 look-ahead bias。

实时口径：
- +1s 决策，只能使用 T0~T0+1s 的非 KOL 成交；
- +3s 决策，只能使用 T0~T0+3s；
- +5s 决策，只能使用 T0~T0+5s；
- 入场仍为决策时点之后第一笔真实市场成交；
- 收益统一改为“入场后持有 N 秒”，避免把 T0+30s 与不同决策延迟混在一起；
- 退出规则只使用入场之后的秒级成交路径，不使用未来 KOL 首卖或未来 probe 定义。

结果仍是历史市场参考价格毛收益，不是指定资金规模的可执行 VWAP。
"""

from __future__ import annotations

import importlib.util
import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

V3_PATH = Path(__file__).with_name("KOL首买事件研究_v3.py")
spec = importlib.util.spec_from_file_location("kol_first_buy_v3", V3_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load V3 module: {V3_PATH}")
v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)

v2 = v3.v2
base = v3.base
DECISIONS_S = (1, 3, 5)
HOLD_S = (5, 10, 20, 30, 60)

_v3_analyze_event = v3.analyze_event
_v3_summarize = v3.summarize


def med(xs: List[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def mean(xs: List[float]) -> Optional[float]:
    return round(statistics.fmean(xs), 3) if xs else None


def metric(xs: List[float]) -> Dict[str, Any]:
    return {
        "n": len(xs),
        "median_bps": med(xs),
        "mean_bps": mean(xs),
        "win_rate": round(sum(x > 0 for x in xs) / len(xs), 4) if xs else None,
    }


def bars_for_event(event: Dict[str, Any], args) -> List[Dict[str, float]]:
    t0 = base.inum(event.get("t0_ms"))
    start = t0 - args.pre_window_seconds * 1000
    end = t0 + args.post_window_seconds * 1000
    key = (str(event["chain_id"]), str(event["mint"]), start, end, int(args.max_market_pages))
    with v3._cache_lock:
        cached = v3._window_cache.get(key)
    if not cached:
        return []
    market, complete, _ = cached
    if not complete:
        return []
    bars, _, _ = v3.build_trade_bars(market)
    return bars


def mark_after_hold(
    bars: List[Dict[str, float]], entry_ts: int, hold_s: int, tolerance_ms: int = 5000
) -> Optional[float]:
    target = entry_ts + hold_s * 1000
    b = v3.last_bar_before(bars, target, lookback_ms=tolerance_ms)
    if not b:
        return None
    # 不允许拿入场前的 bar 冒充未来 mark。
    if int(b["timestamp"]) < entry_ts:
        return None
    return float(b["close"])


def path_closes_after(
    bars: List[Dict[str, float]], entry_ts: int, max_hold_s: int
) -> List[Tuple[int, float]]:
    end = entry_ts + max_hold_s * 1000
    return [
        (int(b["timestamp"]), float(b["close"]))
        for b in bars
        if entry_ts <= int(b["timestamp"]) <= end and float(b["close"]) > 0
    ]


def simulate_exit_rule(
    bars: List[Dict[str, float]],
    entry_price: float,
    entry_ts: int,
    *,
    activation_pct: Optional[float],
    trail_pct: Optional[float],
    stop_loss_pct: float,
    max_hold_s: int,
    take_profit_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """用 1 秒 close 做保守、可复现的规则模拟。

    为避免同一秒 high/low 先后顺序未知，不用 bar high/low 判定退出，只用 close。
    """
    xs = path_closes_after(bars, entry_ts, max_hold_s)
    if not xs:
        return {"exit_bps": None, "exit_after_s": None, "reason": None}

    peak = entry_price
    activated = False
    last_ts, last_price = xs[-1]
    for ts, price in xs:
        ret = price / entry_price - 1.0
        if ret <= stop_loss_pct:
            return {
                "exit_bps": round(ret * 10000, 3),
                "exit_after_s": round((ts - entry_ts) / 1000, 3),
                "reason": "stop_loss",
            }
        if take_profit_pct is not None and ret >= take_profit_pct:
            return {
                "exit_bps": round(ret * 10000, 3),
                "exit_after_s": round((ts - entry_ts) / 1000, 3),
                "reason": "take_profit",
            }

        if price > peak:
            peak = price
        peak_ret = peak / entry_price - 1.0
        if activation_pct is not None and peak_ret >= activation_pct:
            activated = True
        if activated and trail_pct is not None and peak > 0:
            drawdown = price / peak - 1.0
            if drawdown <= -trail_pct:
                return {
                    "exit_bps": round(ret * 10000, 3),
                    "exit_after_s": round((ts - entry_ts) / 1000, 3),
                    "reason": "trailing_stop",
                }

    ret = last_price / entry_price - 1.0
    return {
        "exit_bps": round(ret * 10000, 3),
        "exit_after_s": round((last_ts - entry_ts) / 1000, 3),
        "reason": "time_stop",
    }


EXIT_RULES = {
    "trail10_10_sl15_t60": {
        "activation_pct": 0.10, "trail_pct": 0.10, "stop_loss_pct": -0.15,
        "max_hold_s": 60, "take_profit_pct": None,
    },
    "trail20_15_sl20_t90": {
        "activation_pct": 0.20, "trail_pct": 0.15, "stop_loss_pct": -0.20,
        "max_hold_s": 90, "take_profit_pct": None,
    },
    "tp20_sl15_t30": {
        "activation_pct": None, "trail_pct": None, "stop_loss_pct": -0.15,
        "max_hold_s": 30, "take_profit_pct": 0.20,
    },
}


def analyze_event(kol, pnl_item, history, history_complete, history_pages, args):
    event = _v3_analyze_event(kol, pnl_item, history, history_complete, history_pages, args)
    if not event:
        return None
    event["analysis_version"] = 4
    bars = bars_for_event(event, args)

    for d in DECISIONS_S:
        # 这些特征在决策时点已经全部可见。
        event[f"decision_{d}s_net_buy_usd"] = event.get(f"followers_{d}s_net_buy_usd")
        event[f"decision_{d}s_unique_buyers"] = event.get(f"followers_{d}s_unique_buyers")
        event[f"decision_{d}s_buy_usd"] = event.get(f"followers_{d}s_buy_usd")
        event[f"decision_{d}s_sell_usd"] = event.get(f"followers_{d}s_sell_usd")
        event[f"decision_{d}s_entry_premium_bps"] = event.get(f"entry_{d}s_vs_kol_entry_bps")

        entry_price = event.get(f"entry_{d}s_price")
        entry_ts = base.inum(event.get(f"entry_{d}s_trade_ts"))
        if entry_price and entry_ts and bars:
            for hold in HOLD_S:
                mark = mark_after_hold(bars, entry_ts, hold)
                ret = base.bps(float(entry_price), mark)
                event[f"decision_{d}s_hold_{hold}s_bps"] = round(ret, 3) if ret is not None else None
            for name, rule in EXIT_RULES.items():
                result = simulate_exit_rule(bars, float(entry_price), entry_ts, **rule)
                event[f"decision_{d}s_{name}_bps"] = result["exit_bps"]
                event[f"decision_{d}s_{name}_exit_after_s"] = result["exit_after_s"]
                event[f"decision_{d}s_{name}_reason"] = result["reason"]
        else:
            for hold in HOLD_S:
                event[f"decision_{d}s_hold_{hold}s_bps"] = None
            for name in EXIT_RULES:
                event[f"decision_{d}s_{name}_bps"] = None
                event[f"decision_{d}s_{name}_exit_after_s"] = None
                event[f"decision_{d}s_{name}_reason"] = None
    return event


def bucket(events: List[Dict[str, Any]], d: int, predicate) -> Dict[str, Any]:
    es = [e for e in events if predicate(e)]
    out: Dict[str, Any] = {"events": len(es)}
    for hold in (10, 20, 30, 60):
        key = f"decision_{d}s_hold_{hold}s_bps"
        out[f"hold_{hold}s"] = metric([float(e[key]) for e in es if e.get(key) is not None])
    out["mfe"] = metric([
        float(e[f"entry_{d}s_mfe_bps"]) for e in es if e.get(f"entry_{d}s_mfe_bps") is not None
    ])
    for name in EXIT_RULES:
        key = f"decision_{d}s_{name}_bps"
        out[name] = metric([float(e[key]) for e in es if e.get(key) is not None])
    return out


def decision_summary(events: List[Dict[str, Any]], d: int) -> Dict[str, Any]:
    valid = [
        e for e in events
        if e.get(f"entry_{d}s_price") is not None
        and e.get(f"decision_{d}s_net_buy_usd") is not None
        and e.get("path_source") is not None
    ]
    out: Dict[str, Any] = {
        "valid_n": len(valid),
        "net_buy_usd_median": med([float(e[f"decision_{d}s_net_buy_usd"]) for e in valid]),
        "unique_buyers_median": med([float(e[f"decision_{d}s_unique_buyers"]) for e in valid]),
        "entry_premium_bps": metric([
            float(e[f"decision_{d}s_entry_premium_bps"]) for e in valid
            if e.get(f"decision_{d}s_entry_premium_bps") is not None
        ]),
        "unconditional": bucket(valid, d, lambda e: True),
        "net_buy_buckets": {},
        "premium_buckets": {},
        "combined_filters": {},
    }

    net_defs = [
        ("non_positive", -math.inf, 0.000001),
        ("0_100", 0.000001, 100),
        ("100_300", 100, 300),
        ("300_500", 300, 500),
        ("500_plus", 500, math.inf),
    ]
    for label, lo, hi in net_defs:
        out["net_buy_buckets"][label] = bucket(
            valid, d, lambda e, lo=lo, hi=hi: lo <= float(e[f"decision_{d}s_net_buy_usd"]) < hi
        )

    premium_defs = [
        ("le_5pct", -math.inf, 500),
        ("5_15pct", 500, 1500),
        ("15_30pct", 1500, 3000),
        ("30pct_plus", 3000, math.inf),
    ]
    for label, lo, hi in premium_defs:
        out["premium_buckets"][label] = bucket(
            valid, d,
            lambda e, lo=lo, hi=hi: e.get(f"decision_{d}s_entry_premium_bps") is not None
            and lo <= float(e[f"decision_{d}s_entry_premium_bps"]) < hi
        )

    # 少量、预先声明的实时可用组合；不在这里做网格搜索。
    combos = {
        "net300plus_premium_le30pct": lambda e: (
            float(e[f"decision_{d}s_net_buy_usd"]) >= 300
            and e.get(f"decision_{d}s_entry_premium_bps") is not None
            and float(e[f"decision_{d}s_entry_premium_bps"]) <= 3000
        ),
        "net500plus_premium_le30pct": lambda e: (
            float(e[f"decision_{d}s_net_buy_usd"]) >= 500
            and e.get(f"decision_{d}s_entry_premium_bps") is not None
            and float(e[f"decision_{d}s_entry_premium_bps"]) <= 3000
        ),
        "net500plus_premium_le15pct": lambda e: (
            float(e[f"decision_{d}s_net_buy_usd"]) >= 500
            and e.get(f"decision_{d}s_entry_premium_bps") is not None
            and float(e[f"decision_{d}s_entry_premium_bps"]) <= 1500
        ),
    }
    for name, pred in combos.items():
        out["combined_filters"][name] = bucket(valid, d, pred)
    return out


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _v3_summarize(events)
    out["analysis_version"] = 4
    out["decision_aligned"] = {str(d): decision_summary(events, d) for d in DECISIONS_S}

    # 按 KOL 给 +1/+3/+5 决策的最基础实时指标，方便后续做白名单。
    out["decision_by_kol"] = {}
    for name in sorted({e["kol"] for e in events}):
        es = [e for e in events if e["kol"] == name]
        kd: Dict[str, Any] = {}
        for d in DECISIONS_S:
            valid = [e for e in es if e.get(f"decision_{d}s_hold_10s_bps") is not None]
            kd[str(d)] = {
                "n": len(valid),
                "net_buy_median": med([
                    float(e[f"decision_{d}s_net_buy_usd"]) for e in valid
                    if e.get(f"decision_{d}s_net_buy_usd") is not None
                ]),
                "premium": metric([
                    float(e[f"decision_{d}s_entry_premium_bps"]) for e in valid
                    if e.get(f"decision_{d}s_entry_premium_bps") is not None
                ]),
                "hold10": metric([float(e[f"decision_{d}s_hold_10s_bps"]) for e in valid]),
                "hold30": metric([
                    float(e[f"decision_{d}s_hold_30s_bps"]) for e in valid
                    if e.get(f"decision_{d}s_hold_30s_bps") is not None
                ]),
                "trail10": metric([
                    float(e[f"decision_{d}s_trail10_10_sl15_t60_bps"]) for e in valid
                    if e.get(f"decision_{d}s_trail10_10_sl15_t60_bps") is not None
                ]),
            }
        out["decision_by_kol"][name] = kd
    return out


v2.analyze_event = analyze_event
v2.summarize = summarize

if __name__ == "__main__":
    print("V4_ACTIVE 4")
    v2.main()
