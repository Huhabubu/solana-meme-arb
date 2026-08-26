#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V5：冻结 V4 规则，在不重叠的较老历史区间做时间鲁棒性验证。

注意：规则是在最近样本研究后确定，再向更老历史验证，因此严格来说不是
forward out-of-sample；它是 non-overlapping temporal robustness check。
真正的 forward OOS 需要从规则冻结之后继续收集未来事件。
"""

from __future__ import annotations

import importlib.util
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

V4_PATH = Path(__file__).with_name("KOL首买事件研究_v4.py")
spec = importlib.util.spec_from_file_location("kol_first_buy_v4", V4_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load V4 module: {V4_PATH}")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)

v2 = v4.v2
MIN_AGE_HOURS = 168.0  # 与 V4 最近 7 天训练/探索样本完全不重叠
_v4_analyze_event = v4.analyze_event
_v4_summarize = v4.summarize


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


def analyze_event(kol, pnl_item, history, history_complete, history_pages, args):
    event = _v4_analyze_event(kol, pnl_item, history, history_complete, history_pages, args)
    if not event:
        return None
    age = float(event.get("event_age_hours") or 0)
    if age < MIN_AGE_HOURS:
        return None
    event["analysis_version"] = 5
    event["temporal_validation_min_age_hours"] = MIN_AGE_HOURS
    return event


def rule_metrics(events: List[Dict[str, Any]], decision: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"events": len(events)}
    for hold in (10, 20, 30, 60):
        key = f"decision_{decision}s_hold_{hold}s_bps"
        out[f"hold_{hold}s"] = metric([float(e[key]) for e in events if e.get(key) is not None])
    for name in v4.EXIT_RULES:
        key = f"decision_{decision}s_{name}_bps"
        out[name] = metric([float(e[key]) for e in events if e.get(key) is not None])
    mfe_key = f"entry_{decision}s_mfe_bps"
    out["mfe"] = metric([float(e[mfe_key]) for e in events if e.get(mfe_key) is not None])
    out["mfe_time_median_s"] = med([
        float(e[f"entry_{decision}s_mfe_after_entry_s"])
        for e in events if e.get(f"entry_{decision}s_mfe_after_entry_s") is not None
    ])
    return out


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _v4_summarize(events)
    out["analysis_version"] = 5
    out["temporal_validation_window"] = {
        "min_age_hours": MIN_AGE_HOURS,
        "max_age_hours_from_cli": max([float(e.get("event_age_hours") or 0) for e in events], default=None),
        "event_count": len(events),
    }

    # 冻结规则 A：阿峰 +1s fast-follow，不再根据老样本改阈值。
    afeng = [
        e for e in events
        if e.get("kol") == "阿峰" and e.get("decision_1s_hold_10s_bps") is not None
    ]

    # 冻结规则 B/C：+5s 时已经观察到的净买入 + 当前追价溢价。
    generic300 = [
        e for e in events
        if e.get("decision_5s_net_buy_usd") is not None
        and e.get("decision_5s_entry_premium_bps") is not None
        and float(e["decision_5s_net_buy_usd"]) >= 300
        and float(e["decision_5s_entry_premium_bps"]) <= 3000
        and e.get("decision_5s_hold_10s_bps") is not None
    ]
    generic500 = [
        e for e in events
        if e.get("decision_5s_net_buy_usd") is not None
        and e.get("decision_5s_entry_premium_bps") is not None
        and float(e["decision_5s_net_buy_usd"]) >= 500
        and float(e["decision_5s_entry_premium_bps"]) <= 3000
        and e.get("decision_5s_hold_10s_bps") is not None
    ]

    out["frozen_rules"] = {
        "A_afeng_fast_1s": rule_metrics(afeng, 1),
        "B_generic_5s_net300_premium30": rule_metrics(generic300, 5),
        "C_generic_5s_net500_premium30": rule_metrics(generic500, 5),
    }

    # 只用于看是否被某个 KOL 单独支撑，不改变规则。
    out["frozen_rule_event_distribution"] = {
        "A": {name: sum(e.get("kol") == name for e in afeng) for name in sorted({e.get("kol") for e in afeng})},
        "B": {name: sum(e.get("kol") == name for e in generic300) for name in sorted({e.get("kol") for e in generic300})},
        "C": {name: sum(e.get("kol") == name for e in generic500) for name in sorted({e.get("kol") for e in generic500})},
    }
    return out


v2.analyze_event = analyze_event
v2.summarize = summarize

if __name__ == "__main__":
    print("V5_TEMPORAL_ACTIVE 5")
    v2.main()
