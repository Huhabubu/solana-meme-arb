#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿峰扩样本研究：分页发现更多 Mint，并按时间分层复核 +1s fast-follow。

保持 V4 的事件定义、价格路径和退出规则不变，仅做两件事：
1. pnl/token-list 使用 hasNext + offset 分页，突破单页约 50 Mint 的发现上限；
2. 在 summary 中加入 0-7d / 7-14d / 14-30d 的阿峰 +1s 时间分层。

这仍是历史市场参考价格毛收益，不是指定资金规模的可执行 VWAP。
"""

from __future__ import annotations

import importlib.util
import statistics
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

V4_PATH = Path(__file__).with_name("KOL首买事件研究_v4.py")
spec = importlib.util.spec_from_file_location("kol_first_buy_v4_afeng", V4_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load V4 module: {V4_PATH}")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)

base = v4.base
v2 = v4.v2
_original_summarize = v4.summarize


def fetch_recent_mints_paged(wallet: str, chain_id: str, limit: int) -> List[Dict[str, Any]]:
    """按 OKX 返回的 hasNext/offset 分页发现近期活跃 Mint。"""
    wanted = max(1, int(limit))
    page_size = min(50, wanted)
    offset = 0
    out: List[Dict[str, Any]] = []
    seen = set()

    while len(out) < wanted:
        params = {
            "walletAddress": wallet,
            "chainId": chain_id,
            "isAsc": "false",
            "sortType": "1",
            "offset": str(offset),
            "limit": str(min(page_size, wanted - len(out))),
            "filterRisk": "true",
            "filterSmallBalance": "false",
            "t": str(int(time.time() * 1000)),
        }
        status, body = base.request_json(
            base.PNL_URL + "?" + urllib.parse.urlencode(params),
            referer="https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
        )
        if status != 200 or str(body.get("code")) != "0":
            raise RuntimeError(
                f"pnl failed status={status} code={body.get('code')} msg={body.get('msg')} offset={offset}"
            )

        data = body.get("data") or {}
        items = data.get("tokenList") or []
        if not items:
            break

        for item in items:
            mint = str(item.get("tokenContractAddress") or "")
            key = mint or str(item.get("rowId") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= wanted:
                break

        has_next = str(data.get("hasNext")).lower() in ("true", "1")
        if not has_next or len(out) >= wanted:
            break

        next_offset = base.inum(data.get("offset"))
        if next_offset <= offset:
            next_offset = offset + len(items)
        if next_offset <= offset:
            break
        offset = next_offset

    print(f"PNL_PAGED_MINTS requested={wanted} returned={len(out)} final_offset={offset}")
    return out


def _metric(values: List[float]) -> Dict[str, Any]:
    xs = [float(x) for x in values]
    return {
        "n": len(xs),
        "median_bps": round(statistics.median(xs), 3) if xs else None,
        "mean_bps": round(statistics.fmean(xs), 3) if xs else None,
        "win_rate": round(sum(x > 0 for x in xs) / len(xs), 4) if xs else None,
    }


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 3) if values else None


def _time_bucket(events: List[Dict[str, Any]], lo_h: float, hi_h: float) -> Dict[str, Any]:
    es = [
        e for e in events
        if lo_h <= float(e.get("event_age_hours") or 0) < hi_h
        and e.get("decision_1s_hold_10s_bps") is not None
    ]
    out: Dict[str, Any] = {
        "events": len(es),
        "event_age_hours_min": min([float(e["event_age_hours"]) for e in es], default=None),
        "event_age_hours_max": max([float(e["event_age_hours"]) for e in es], default=None),
        "entry_premium": _metric([
            float(e["decision_1s_entry_premium_bps"])
            for e in es if e.get("decision_1s_entry_premium_bps") is not None
        ]),
        "net_buy_usd_median": _median([
            float(e["decision_1s_net_buy_usd"])
            for e in es if e.get("decision_1s_net_buy_usd") is not None
        ]),
        "first_buy_usd_median": _median([
            float(e["first_buy_usd"]) for e in es if e.get("first_buy_usd") is not None
        ]),
    }
    for hold in (5, 10, 20, 30, 60):
        key = f"decision_1s_hold_{hold}s_bps"
        out[f"hold_{hold}s"] = _metric([float(e[key]) for e in es if e.get(key) is not None])
    for rule in ("trail10_10_sl15_t60", "trail20_15_sl20_t90", "tp20_sl15_t30"):
        key = f"decision_1s_{rule}_bps"
        out[rule] = _metric([float(e[key]) for e in es if e.get(key) is not None])
    return out


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _original_summarize(events)
    out["afeng_expanded"] = {
        "paged_mint_discovery": True,
        "valid_fast_1s_events": sum(e.get("decision_1s_hold_10s_bps") is not None for e in events),
        "time_buckets": {
            "0_7d": _time_bucket(events, 0, 168),
            "7_14d": _time_bucket(events, 168, 336),
            "14_30d": _time_bucket(events, 336, 720),
        },
    }
    return out


base.fetch_recent_mints = fetch_recent_mints_paged
v2.summarize = summarize

if __name__ == "__main__":
    print("AFENG_EXPANDED_ACTIVE 1")
    v2.main()
