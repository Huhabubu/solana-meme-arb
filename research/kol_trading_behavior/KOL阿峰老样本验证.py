#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿峰 7-30 天老样本验证。

目的：避免 pnl/token-list 按近期活跃 Mint 排序造成的时间偏差，直接从 OKX 钱包级
trade-history 向过去分页，发现 7-30 天内阿峰实际买过的 Mint，再复用 V4 的完整
KOL×Mint 历史、真实逐笔入场、300 秒价格路径和退出规则。

冻结规则（不根据本脚本结果调参）：
A. 阿峰首买后 +1s 第一笔真实市场成交跟入；
B. 同 A，但 +1s 时跟入价相对 KOL 首买价溢价 <= 5%（500 bps）。

这是 non-overlapping historical robustness check，不是真正 forward OOS；所有收益仍为
历史市场参考价格毛收益，未加入指定资金规模 VWAP、Gas、税费、MEV。
"""

from __future__ import annotations

import importlib.util
import statistics
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

V4_PATH = Path(__file__).with_name("KOL首买事件研究_v4.py")
spec = importlib.util.spec_from_file_location("kol_first_buy_v4_afeng_old", V4_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load V4 module: {V4_PATH}")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)

base = v4.base
v2 = v4.v2
_original_analyze = v4.analyze_event
_original_summarize = v4.summarize

WALLET_HISTORY_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/pnl/wallet-profile/trade-history"
MIN_AGE_HOURS = 168.0
MAX_AGE_HOURS = 720.0


def _is_true(v: Any) -> bool:
    return str(v).lower() in ("true", "1")


def discover_old_mints(wallet: str, chain_id: str, limit: int) -> List[Dict[str, Any]]:
    """从钱包级交易历史发现 7-30 天内出现 BUY 的 Mint。

    钱包历史按新到旧分页；扫描到 30 天以前即停止。若候选过多，则按
    7-14 / 14-21 / 21-30 天三个时间层近似均衡抽取，避免又被靠近 7 天的事件占满。
    """
    now_ms = int(time.time() * 1000)
    newest_ms = now_ms - int(MIN_AGE_HOURS * 3_600_000)
    oldest_ms = now_ms - int(MAX_AGE_HOURS * 3_600_000)
    wanted = max(1, int(limit))

    global_index: Optional[str] = None
    block_time_pagination: Optional[str] = None
    has_next = True
    pages = 0
    rows_seen = 0
    reached_old_boundary = False

    # mint -> representative BUY row。保留区间内最早看到（时间更老）的 BUY，便于时间分层。
    candidates: Dict[str, Dict[str, Any]] = {}

    while has_next and pages < 1000:
        params: Dict[str, Any] = {
            "walletAddress": wallet,
            "chainId": chain_id,
            "pageSize": "50",
            "tradeType": "1,2",
            "filterRisk": "true",
            "t": str(int(time.time() * 1000)),
        }
        if global_index and block_time_pagination:
            params["globalIndex"] = global_index
            params["blockTimePagination"] = block_time_pagination

        status, body = base.request_json(
            WALLET_HISTORY_URL + "?" + urllib.parse.urlencode(params),
            referer="https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
        )
        if status != 200:
            raise RuntimeError(f"wallet history status={status} page={pages + 1}")

        data = body.get("data") or {}
        rows = data.get("rows") or []
        pages += 1
        rows_seen += len(rows)
        if not rows:
            break

        oldest_on_page = None
        for row in rows:
            ts = base.inum(row.get("blockTime"))
            if ts <= 0:
                continue
            oldest_on_page = ts if oldest_on_page is None else min(oldest_on_page, ts)
            if ts < oldest_ms:
                reached_old_boundary = True
                continue
            if ts >= newest_ms:
                continue
            if str(row.get("type")) != "1":
                continue
            mint = str(row.get("tokenContractAddress") or "")
            if not mint:
                continue
            prev = candidates.get(mint)
            if prev is None or ts < base.inum(prev.get("_discovery_buy_ms")):
                candidates[mint] = {
                    "tokenContractAddress": mint,
                    "tokenSymbol": row.get("tokenSymbol"),
                    "_discovery_buy_ms": ts,
                    "_discovery_tx_hash": row.get("txHash"),
                    "_discovery_source": "wallet-profile/trade-history",
                }

        if reached_old_boundary or (oldest_on_page is not None and oldest_on_page < oldest_ms):
            break

        has_next = _is_true(data.get("hasNext"))
        if not has_next:
            break
        last = rows[-1]
        next_global = last.get("globalIndex")
        next_block = last.get("blockTime")
        if not next_global or not next_block:
            break
        if str(next_global) == str(global_index) and str(next_block) == str(block_time_pagination):
            break
        global_index = str(next_global)
        block_time_pagination = str(next_block)
        time.sleep(0.02)

    items = list(candidates.values())
    # 按发现 BUY 的年龄分三层；若超过 wanted，则尽量均衡取样。
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        age_h = (now_ms - base.inum(item.get("_discovery_buy_ms"))) / 3_600_000
        if age_h < 336:
            key = "7_14d"
        elif age_h < 504:
            key = "14_21d"
        else:
            key = "21_30d"
        item["_discovery_age_hours"] = round(age_h, 4)
        buckets[key].append(item)

    for xs in buckets.values():
        xs.sort(key=lambda x: base.inum(x.get("_discovery_buy_ms")), reverse=True)

    if len(items) > wanted:
        selected: List[Dict[str, Any]] = []
        keys = ("7_14d", "14_21d", "21_30d")
        # round-robin，保证时间层不被单一区间垄断。
        idx = 0
        while len(selected) < wanted:
            added = False
            for key in keys:
                xs = buckets.get(key) or []
                if idx < len(xs) and len(selected) < wanted:
                    selected.append(xs[idx])
                    added = True
            if not added:
                break
            idx += 1
        items = selected

    print(
        "AFENG_OLD_DISCOVERY "
        f"pages={pages} rows={rows_seen} unique_mints={len(candidates)} selected={len(items)} "
        f"7_14d={len(buckets.get('7_14d') or [])} "
        f"14_21d={len(buckets.get('14_21d') or [])} "
        f"21_30d={len(buckets.get('21_30d') or [])} "
        f"reached_30d={reached_old_boundary}"
    )
    return items


def analyze_event(kol, pnl_item, history, history_complete, history_pages, args):
    event = _original_analyze(kol, pnl_item, history, history_complete, history_pages, args)
    if not event:
        return None
    age = float(event.get("event_age_hours") or 0)
    if not (MIN_AGE_HOURS <= age <= MAX_AGE_HOURS):
        return None
    event["old_sample_validation"] = True
    event["discovery_buy_ms"] = pnl_item.get("_discovery_buy_ms")
    event["discovery_age_hours"] = pnl_item.get("_discovery_age_hours")
    return event


def _metric(values: List[float]) -> Dict[str, Any]:
    xs = [float(v) for v in values]
    return {
        "n": len(xs),
        "median_bps": round(statistics.median(xs), 3) if xs else None,
        "mean_bps": round(statistics.fmean(xs), 3) if xs else None,
        "win_rate": round(sum(v > 0 for v in xs) / len(xs), 4) if xs else None,
    }


def _rule_metrics(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"events": len(events)}
    if events:
        ages = [float(e.get("event_age_hours") or 0) for e in events]
        out["age_hours_min"] = round(min(ages), 3)
        out["age_hours_max"] = round(max(ages), 3)
    for hold in (5, 10, 20, 30, 60):
        key = f"decision_1s_hold_{hold}s_bps"
        out[f"hold_{hold}s"] = _metric([float(e[key]) for e in events if e.get(key) is not None])
    for rule in ("trail10_10_sl15_t60", "trail20_15_sl20_t90", "tp20_sl15_t30"):
        key = f"decision_1s_{rule}_bps"
        out[rule] = _metric([float(e[key]) for e in events if e.get(key) is not None])
    out["entry_premium"] = _metric([
        float(e["decision_1s_entry_premium_bps"])
        for e in events if e.get("decision_1s_entry_premium_bps") is not None
    ])
    out["mfe"] = _metric([
        float(e["entry_1s_mfe_bps"]) for e in events if e.get("entry_1s_mfe_bps") is not None
    ])
    return out


def _age_bucket(events: List[Dict[str, Any]], lo: float, hi: float) -> Dict[str, Any]:
    es = [e for e in events if lo <= float(e.get("event_age_hours") or 0) < hi]
    return {
        "all_rule_A": _rule_metrics(es),
        "rule_B_premium_le5pct": _rule_metrics([
            e for e in es
            if e.get("decision_1s_entry_premium_bps") is not None
            and float(e["decision_1s_entry_premium_bps"]) <= 500
        ]),
    }


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _original_summarize(events)
    valid = [e for e in events if e.get("decision_1s_hold_10s_bps") is not None]
    rule_b = [
        e for e in valid
        if e.get("decision_1s_entry_premium_bps") is not None
        and float(e["decision_1s_entry_premium_bps"]) <= 500
    ]
    out["afeng_old_validation"] = {
        "window": "7-30d",
        "rules_frozen_before_old_sample": True,
        "rule_A_fast_1s": _rule_metrics(valid),
        "rule_B_fast_1s_premium_le5pct": _rule_metrics(rule_b),
        "time_buckets": {
            "7_14d": _age_bucket(valid, 168, 336),
            "14_21d": _age_bucket(valid, 336, 504),
            "21_30d": _age_bucket(valid, 504, 721),
        },
    }
    return out


base.fetch_recent_mints = discover_old_mints
v2.analyze_event = analyze_event
v2.summarize = summarize

if __name__ == "__main__":
    print("AFENG_OLD_VALIDATION_ACTIVE 1")
    v2.main()
