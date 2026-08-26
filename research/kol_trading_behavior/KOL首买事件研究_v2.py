#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 首买事件研究 V2（只读）。

V2 核心口径：
1. KOL 建仓价：KOL 历史首笔 BUY 的真实逐笔成交价。
2. 跟随者 +1/+2/+3/+5/+10s 建仓参考价：目标时间之后第一笔真实市场成交价；
   不再使用 1 秒 K 线 close 作为建仓价。
3. 固定观察点 +5/+10/+20/+30/+60/+300s：使用对应 1 秒 K 线 close。
4. MFE/MAE：从实际参考建仓成交时刻到 T0+300s，分别使用 1 秒 K 线最高 high / 最低 low。
5. 额外记录 MFE/MAE 出现时间、MFE 到 +300s 的利润回吐。
6. 全市场成交通过 OKX startTime/endTime + dataId=epoch_ms 直接跳转历史窗口。

注意：跟随者建仓价仍只是“第一笔真实市场成交参考价”，没有模拟指定下单金额带来的
滑点、价格冲击、Gas、Token 税、MEV 和路由差异，因此不等于真实可执行 VWAP。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_PATH = Path(__file__).with_name("KOL首买事件研究.py")
spec = importlib.util.spec_from_file_location("kol_first_buy_v1", BASE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load base module: {BASE_PATH}")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

DELAYS_S = (1, 2, 3, 5, 10)
HORIZONS_S = (5, 10, 20, 30, 60, 300)
PRESSURE_WINDOWS_S = (1, 3, 5, 10)


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


def quantile(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return round(ys[0], 3)
    pos = (len(ys) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(ys[lo], 3)
    v = ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)
    return round(v, 3)


def candle_close(rows: List[Dict[str, float]], target_ms: int, tolerance_ms: int = 1500) -> Optional[float]:
    """固定时点价格只取目标附近 1 秒 K 线 close。"""
    best: Optional[Tuple[int, float]] = None
    for r in rows:
        ts = int(r["timestamp"])
        d = abs(ts - target_ms)
        close = float(r.get("close") or 0)
        if d <= tolerance_ms and close > 0 and (best is None or d < best[0]):
            best = (d, close)
    return best[1] if best else None


def excursion_metrics(
    candles: List[Dict[str, float]],
    entry_price: float,
    entry_ts: int,
    t0: int,
    end_ms: int,
) -> Dict[str, Optional[float]]:
    future = [
        r for r in candles
        if entry_ts <= int(r["timestamp"]) <= end_ms
        and float(r.get("high") or 0) > 0
        and float(r.get("low") or 0) > 0
    ]
    if not future:
        return {
            "mfe_bps": None, "mae_bps": None,
            "mfe_after_t0_s": None, "mfe_after_entry_s": None,
            "mae_after_t0_s": None, "mae_after_entry_s": None,
            "mfe_price": None, "mae_price": None,
        }

    max_row = max(future, key=lambda r: float(r["high"]))
    min_row = min(future, key=lambda r: float(r["low"]))
    max_p = float(max_row["high"])
    min_p = float(min_row["low"])
    max_ts = int(max_row["timestamp"])
    min_ts = int(min_row["timestamp"])
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


def fetch_entry_window(chain_id: str, mint: str, t0: int, args: argparse.Namespace):
    # 覆盖 +1s 到 +10s，并给最后一个目标最多 5 秒等待下一笔真实成交。
    start = t0 + 1000
    end = t0 + args.entry_window_seconds * 1000
    return base.fetch_market_window(chain_id, mint, start, end, args.max_entry_pages)


def analyze_event(
    kol: Dict[str, Any],
    pnl_item: Dict[str, Any],
    history: List[Dict[str, Any]],
    history_complete: bool,
    history_pages: int,
    args: argparse.Namespace,
) -> Optional[Dict[str, Any]]:
    wallet = kol["address"]
    mint = pnl_item.get("tokenContractAddress")
    chain_id = kol["chain_id"]
    chron = sorted(history, key=lambda r: base.inum(r.get("timestamp")))
    buys = [r for r in chron if base.is_buy(r)]
    if not buys:
        return None

    first = buys[0]
    first_usd = base.fnum(first.get("volume"))
    first_price = base.row_price(first)
    t0 = base.inum(first.get("timestamp"))
    if first_usd < args.min_first_buy_usd or not first_price or not t0:
        return None

    age_h = (int(time.time() * 1000) - t0) / 3_600_000
    if age_h < 0 or age_h > args.max_event_age_hours:
        return None

    later = [r for r in chron if base.inum(r.get("timestamp")) > t0]
    later_sells = [r for r in later if not base.is_buy(r)]
    first_sell = later_sells[0] if later_sells else None
    first_sell_ts = base.inum(first_sell.get("timestamp")) if first_sell else None
    first_sell_ret = base.bps(first_price, base.row_price(first_sell))

    before_sell_buys = [
        r for r in later
        if base.is_buy(r) and (first_sell_ts is None or base.inum(r.get("timestamp")) < first_sell_ts)
    ]
    probes = [r for r in before_sell_buys if base.fnum(r.get("volume")) <= first_usd * 0.10]

    path_start = t0 - args.pre_window_seconds * 1000
    path_end = t0 + args.post_window_seconds * 1000

    # 1 秒 K 线只负责后续价格路径，不负责建仓。
    candle_t = time.perf_counter()
    try:
        candles, candle_complete = base.fetch_candles(chain_id, mint, path_start, path_end)
    except Exception:
        candles, candle_complete = [], False
    candle_fetch_s = time.perf_counter() - candle_t

    # 所有事件都抓极小逐笔窗口，确定 +1/+2/+3/+5/+10s 真实参考成交价。
    entry_t = time.perf_counter()
    try:
        entry_market, entry_complete, entry_pages = fetch_entry_window(chain_id, mint, t0, args)
    except Exception:
        entry_market, entry_complete, entry_pages = [], False, 0
    entry_fetch_s = time.perf_counter() - entry_t

    # 仅较新事件抓完整 5 分钟逐笔窗口，做跟单压力。
    market: List[Dict[str, Any]] = []
    market_complete = False
    market_pages = 0
    market_fetch_s: Optional[float] = None
    if age_h <= args.max_follower_event_age_hours:
        market_t = time.perf_counter()
        try:
            market, market_complete, market_pages = base.fetch_market_window(
                chain_id, mint, path_start, path_end, args.max_market_pages
            )
        except Exception:
            market, market_complete, market_pages = [], False, 0
        market_fetch_s = time.perf_counter() - market_t

    event: Dict[str, Any] = {
        "analysis_version": 2,
        "kol": kol["name"],
        "kol_address": wallet,
        "chain": kol["chain"],
        "chain_id": chain_id,
        "tier": kol["tier"],
        "symbol": pnl_item.get("tokenSymbol"),
        "mint": mint,
        "t0_ms": t0,
        "event_age_hours": round(age_h, 4),
        "first_buy_usd": round(first_usd, 6),
        "first_buy_price": first_price,
        "first_buy_tx_hash": base.tx_hash(first),
        "history_rows": len(chron),
        "history_complete": history_complete,
        "history_pages": history_pages,
        "kol_buy_rows": len(buys),
        "kol_sell_rows": len([r for r in chron if not base.is_buy(r)]),
        "first_sell_delay_s": round((first_sell_ts - t0) / 1000, 3) if first_sell_ts else None,
        "first_sell_price": base.row_price(first_sell),
        "first_sell_usd": round(base.fnum(first_sell.get("volume")), 6) if first_sell else None,
        "kol_first_buy_to_first_sell_bps": round(first_sell_ret, 3) if first_sell_ret is not None else None,
        "pre_first_sell_small_probe_buys": len(probes),
        "small_probe_buy_usd": [round(base.fnum(r.get("volume")), 6) for r in probes[:50]],
        "entry_market_rows": len(entry_market),
        "entry_window_complete": entry_complete,
        "entry_pages": entry_pages,
        "entry_fetch_seconds": round(entry_fetch_s, 4),
        "market_rows": len(market),
        "market_window_complete": market_complete,
        "market_pages": market_pages,
        "market_fetch_seconds": round(market_fetch_s, 4) if market_fetch_s is not None else None,
        "candle_rows": len(candles),
        "candle_window_complete": candle_complete,
        "candle_fetch_seconds": round(candle_fetch_s, 4),
        "pnl_reference_total_pnl_usd": base.fnum(pnl_item.get("totalPnl")),
        "pnl_reference_total_pnl_pct": base.fnum(pnl_item.get("totalPnlPercentage")),
    }
    event.update(base.kol_round_metrics(chron))

    if market_complete:
        pre5 = base.non_kol_buy_usd(market, wallet, t0 - 5000, t0)
        post5 = base.non_kol_buy_usd(market, wallet, t0, t0 + 5000)
        event["followers_pre5s_buy_usd"] = round(pre5, 6)
        event["followers_post5s_buy_usd"] = round(post5, 6)
        for sec in PRESSURE_WINDOWS_S:
            event.update(base.pressure(market, wallet, t0, sec))
    else:
        event["followers_pre5s_buy_usd"] = None
        event["followers_post5s_buy_usd"] = None
        for sec in PRESSURE_WINDOWS_S:
            for suffix in ("buy_trades", "unique_buyers", "buy_usd", "sell_usd", "net_buy_usd"):
                event[f"followers_{sec}s_{suffix}"] = None

    mark_300 = candle_close(candles, t0 + 300_000) if candle_complete else None

    for delay in DELAYS_S:
        target = t0 + delay * 1000
        tr = base.first_trade_after(entry_market, target, wait_ms=5000) if entry_complete else None
        entry_price = base.row_price(tr)
        entry_ts = base.inum(tr.get("timestamp")) if tr else None

        event[f"entry_{delay}s_price"] = entry_price
        event[f"entry_{delay}s_trade_ts"] = entry_ts
        event[f"entry_{delay}s_actual_delay_ms"] = (entry_ts - t0) if entry_ts else None
        event[f"entry_{delay}s_wait_after_target_ms"] = (entry_ts - target) if entry_ts else None
        event[f"entry_{delay}s_trade_tx_hash"] = base.tx_hash(tr)

        penalty = base.bps(first_price, entry_price)
        event[f"entry_{delay}s_vs_kol_entry_bps"] = round(penalty, 3) if penalty is not None else None

        # 固定时点全部使用 1 秒 K 线 close。
        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            mark = candle_close(candles, t0 + horizon * 1000) if candle_complete else None
            ret = base.bps(entry_price, mark)
            event[f"entry_{delay}s_to_{horizon}s_bps"] = round(ret, 3) if ret is not None else None

        sell_ret = base.bps(entry_price, base.row_price(first_sell))
        event[f"entry_{delay}s_to_kol_first_sell_bps"] = round(sell_ret, 3) if sell_ret is not None else None

        if entry_price and entry_ts and candle_complete:
            ex = excursion_metrics(candles, entry_price, entry_ts, t0, path_end)
            event[f"entry_{delay}s_mfe_bps"] = ex["mfe_bps"]
            event[f"entry_{delay}s_mae_bps"] = ex["mae_bps"]
            event[f"entry_{delay}s_mfe_after_t0_s"] = ex["mfe_after_t0_s"]
            event[f"entry_{delay}s_mfe_after_entry_s"] = ex["mfe_after_entry_s"]
            event[f"entry_{delay}s_mae_after_t0_s"] = ex["mae_after_t0_s"]
            event[f"entry_{delay}s_mae_after_entry_s"] = ex["mae_after_entry_s"]
            event[f"entry_{delay}s_mfe_price"] = ex["mfe_price"]
            event[f"entry_{delay}s_mae_price"] = ex["mae_price"]
            ret300 = base.bps(entry_price, mark_300)
            mfe = ex["mfe_bps"]
            event[f"entry_{delay}s_to_300s_bps"] = round(ret300, 3) if ret300 is not None else None
            event[f"entry_{delay}s_profit_giveback_to_300s_bps"] = (
                round(float(mfe) - ret300, 3) if mfe is not None and ret300 is not None else None
            )
            event[f"entry_{delay}s_mfe_retained_at_300s_ratio"] = (
                round(ret300 / float(mfe), 4)
                if mfe is not None and float(mfe) > 0 and ret300 is not None else None
            )
        else:
            for suffix in (
                "mfe_bps", "mae_bps", "mfe_after_t0_s", "mfe_after_entry_s",
                "mae_after_t0_s", "mae_after_entry_s", "mfe_price", "mae_price",
                "profit_giveback_to_300s_bps", "mfe_retained_at_300s_ratio",
            ):
                event[f"entry_{delay}s_{suffix}"] = None

    event["entry_1s_valid"] = event.get("entry_1s_price") is not None
    event["analysis_valid"] = bool(event["entry_1s_valid"] and candle_complete)
    return event


def process_candidate(kol: Dict[str, Any], item: Dict[str, Any], args: argparse.Namespace):
    mint = item.get("tokenContractAddress")
    if not mint:
        return "skip", None, None
    try:
        history, complete, pages = base.fetch_kol_history(
            kol["address"], kol["chain_id"], mint, args.max_kol_history_rows
        )
        if not complete:
            return "skip", None, {
                "kol": kol["name"], "mint": mint, "symbol": item.get("tokenSymbol"),
                "stage": "incomplete_history", "history_rows": len(history), "history_pages": pages,
            }
        event = analyze_event(kol, item, history, complete, pages, args)
        return "event" if event else "skip", event, None
    except Exception as exc:
        return "error", None, {
            "kol": kol["name"], "mint": mint, "symbol": item.get("tokenSymbol"),
            "stage": "event", "error": str(exc),
        }


def bucket_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    def s(es: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "events": len(es),
            "entry1_to10": metric([float(e["entry_1s_to_10s_bps"]) for e in es if e.get("entry_1s_to_10s_bps") is not None]),
            "entry1_to30": metric([float(e["entry_1s_to_30s_bps"]) for e in es if e.get("entry_1s_to_30s_bps") is not None]),
            "entry1_mfe": metric([float(e["entry_1s_mfe_bps"]) for e in es if e.get("entry_1s_mfe_bps") is not None]),
            "entry1_mfe_time_median_s": med([float(e["entry_1s_mfe_after_entry_s"]) for e in es if e.get("entry_1s_mfe_after_entry_s") is not None]),
        }

    out: Dict[str, Any] = {}
    valid = [e for e in events if e.get("analysis_valid")]
    follower = [e for e in valid if e.get("market_window_complete")]

    net_defs = [
        ("non_positive", -math.inf, 0.000001),
        ("0_200", 0.000001, 200),
        ("200_500", 200, 500),
        ("500_plus", 500, math.inf),
    ]
    out["followers_5s_net_buy_usd"] = {
        label: s([e for e in follower if lo <= base.fnum(e.get("followers_5s_net_buy_usd")) < hi])
        for label, lo, hi in net_defs
    }
    out["probe_presence"] = {
        "no_probe": s([e for e in valid if base.inum(e.get("pre_first_sell_small_probe_buys")) == 0]),
        "has_probe": s([e for e in valid if base.inum(e.get("pre_first_sell_small_probe_buys")) > 0]),
    }
    return out


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [e for e in events if e.get("analysis_valid")]
    follower_valid = [e for e in valid if e.get("market_window_complete")]
    out: Dict[str, Any] = {
        "analysis_version": 2,
        "event_count": len(events),
        "analysis_valid_event_count": len(valid),
        "follower_valid_event_count": len(follower_valid),
        "kol_own": {
            "first_buy_to_first_sell": metric([
                float(e["kol_first_buy_to_first_sell_bps"]) for e in events
                if e.get("kol_first_buy_to_first_sell_bps") is not None
            ]),
            "closed_round_roi": metric([
                float(e["kol_round_realized_roi_bps"]) for e in events
                if e.get("kol_round_realized_roi_bps") is not None
            ]),
        },
        "delay_metrics": {},
        "by_kol": {},
        "condition_buckets": bucket_summary(events),
    }

    for delay in DELAYS_S:
        es = [e for e in valid if e.get(f"entry_{delay}s_price") is not None]
        d: Dict[str, Any] = {
            "entry_valid_n": len(es),
            "entry_penalty_vs_kol": metric([
                float(e[f"entry_{delay}s_vs_kol_entry_bps"]) for e in es
                if e.get(f"entry_{delay}s_vs_kol_entry_bps") is not None
            ]),
            "entry_wait_after_target_ms_median": med([
                float(e[f"entry_{delay}s_wait_after_target_ms"]) for e in es
                if e.get(f"entry_{delay}s_wait_after_target_ms") is not None
            ]),
            "to_kol_first_sell": metric([
                float(e[f"entry_{delay}s_to_kol_first_sell_bps"]) for e in es
                if e.get(f"entry_{delay}s_to_kol_first_sell_bps") is not None
            ]),
            "mfe": metric([
                float(e[f"entry_{delay}s_mfe_bps"]) for e in es
                if e.get(f"entry_{delay}s_mfe_bps") is not None
            ]),
            "mae": metric([
                float(e[f"entry_{delay}s_mae_bps"]) for e in es
                if e.get(f"entry_{delay}s_mae_bps") is not None
            ]),
            "mfe_time_after_entry_s": {
                "n": len([e for e in es if e.get(f"entry_{delay}s_mfe_after_entry_s") is not None]),
                "median": med([float(e[f"entry_{delay}s_mfe_after_entry_s"]) for e in es if e.get(f"entry_{delay}s_mfe_after_entry_s") is not None]),
                "p25": quantile([float(e[f"entry_{delay}s_mfe_after_entry_s"]) for e in es if e.get(f"entry_{delay}s_mfe_after_entry_s") is not None], 0.25),
                "p75": quantile([float(e[f"entry_{delay}s_mfe_after_entry_s"]) for e in es if e.get(f"entry_{delay}s_mfe_after_entry_s") is not None], 0.75),
            },
            "profit_giveback_to_300s_bps_median": med([
                float(e[f"entry_{delay}s_profit_giveback_to_300s_bps"]) for e in es
                if e.get(f"entry_{delay}s_profit_giveback_to_300s_bps") is not None
            ]),
        }
        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            key = f"entry_{delay}s_to_{horizon}s_bps"
            d[f"to_{horizon}s"] = metric([float(e[key]) for e in es if e.get(key) is not None])
        out["delay_metrics"][str(delay)] = d

    for name in sorted({e["kol"] for e in events}):
        all_es = [e for e in events if e["kol"] == name]
        es = [e for e in valid if e["kol"] == name and e.get("entry_1s_price") is not None]
        out["by_kol"][name] = {
            "events": len(all_es),
            "valid": len(es),
            "kol_first_buy_to_first_sell": metric([
                float(e["kol_first_buy_to_first_sell_bps"]) for e in all_es
                if e.get("kol_first_buy_to_first_sell_bps") is not None
            ]),
            "entry1_penalty": metric([
                float(e["entry_1s_vs_kol_entry_bps"]) for e in es
                if e.get("entry_1s_vs_kol_entry_bps") is not None
            ]),
            "entry1_to10": metric([float(e["entry_1s_to_10s_bps"]) for e in es if e.get("entry_1s_to_10s_bps") is not None]),
            "entry1_to30": metric([float(e["entry_1s_to_30s_bps"]) for e in es if e.get("entry_1s_to_30s_bps") is not None]),
            "entry1_to300": metric([float(e["entry_1s_to_300s_bps"]) for e in es if e.get("entry_1s_to_300s_bps") is not None]),
            "entry1_mfe": metric([float(e["entry_1s_mfe_bps"]) for e in es if e.get("entry_1s_mfe_bps") is not None]),
            "entry1_mae": metric([float(e["entry_1s_mae_bps"]) for e in es if e.get("entry_1s_mae_bps") is not None]),
            "entry1_mfe_time_median_s": med([float(e["entry_1s_mfe_after_entry_s"]) for e in es if e.get("entry_1s_mfe_after_entry_s") is not None]),
            "entry1_to_first_sell": metric([float(e["entry_1s_to_kol_first_sell_bps"]) for e in es if e.get("entry_1s_to_kol_first_sell_bps") is not None]),
        }
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--watchlist", default="research/kol_trading_behavior/kol_watchlist.json")
    p.add_argument("--output-dir", default="research/kol_trading_behavior/out_first_buy_study_v2")
    p.add_argument("--chains", default="56")
    p.add_argument("--tiers", default="primary")
    p.add_argument("--recent-mints", type=int, default=20)
    p.add_argument("--max-kol-history-rows", type=int, default=600)
    p.add_argument("--min-first-buy-usd", type=float, default=100.0)
    p.add_argument("--max-event-age-hours", type=float, default=168.0)
    p.add_argument("--max-follower-event-age-hours", type=float, default=72.0)
    p.add_argument("--pre-window-seconds", type=int, default=5)
    p.add_argument("--post-window-seconds", type=int, default=300)
    p.add_argument("--entry-window-seconds", type=int, default=16)
    p.add_argument("--max-entry-pages", type=int, default=30)
    p.add_argument("--max-market-pages", type=int, default=30)
    p.add_argument("--workers", type=int, default=3)
    return p.parse_args()


def main() -> None:
    a = parse_args()
    chains = {x.strip() for x in a.chains.split(",") if x.strip()}
    tiers = {x.strip() for x in a.tiers.split(",") if x.strip()}
    watch = json.loads(Path(a.watchlist).read_text(encoding="utf-8"))
    kols = [k for k in watch["kols"] if k["chain_id"] in chains and k["tier"] in tiers]

    out_dir = Path(a.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    print("ANALYSIS_VERSION 2")
    print("KOL_COUNT", len(kols))
    print("WORKERS", max(1, a.workers))

    for kol in kols:
        print(f"DISCOVER {kol['name']} {kol['address']} chain={kol['chain_id']}")
        try:
            mints = base.fetch_recent_mints(kol["address"], kol["chain_id"], a.recent_mints)
        except Exception as exc:
            errors.append({"kol": kol["name"], "stage": "recent_mints", "error": str(exc)})
            continue
        for item in mints:
            if item.get("tokenContractAddress"):
                candidates.append((kol, item))

    print("CANDIDATE_COUNT", len(candidates))
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futures = {pool.submit(process_candidate, kol, item, a): (kol, item) for kol, item in candidates}
        for future in as_completed(futures):
            kol, item = futures[future]
            try:
                status, event, err = future.result()
            except Exception as exc:
                status, event, err = "error", None, {
                    "kol": kol["name"], "mint": item.get("tokenContractAddress"),
                    "symbol": item.get("tokenSymbol"), "stage": "future", "error": str(exc),
                }
            if err:
                errors.append(err)
                print("ERROR" if status == "error" else "SKIP", json.dumps(err, ensure_ascii=False))
            if event:
                events.append(event)
                print("EVENT", json.dumps({
                    "kol": event["kol"], "symbol": event["symbol"],
                    "first_buy_usd": event["first_buy_usd"],
                    "entry1_penalty_bps": event.get("entry_1s_vs_kol_entry_bps"),
                    "entry1_to10_bps": event.get("entry_1s_to_10s_bps"),
                    "entry1_mfe_bps": event.get("entry_1s_mfe_bps"),
                    "entry1_mfe_time_s": event.get("entry_1s_mfe_after_entry_s"),
                    "entry1_to300_bps": event.get("entry_1s_to_300s_bps"),
                    "entry1_giveback_bps": event.get("entry_1s_profit_giveback_to_300s_bps"),
                }, ensure_ascii=False))

    events.sort(key=lambda e: (e.get("kol") or "", base.inum(e.get("t0_ms"))))
    summary = summarize(events)
    (out_dir / "events.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in events), encoding="utf-8"
    )
    (out_dir / "errors.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in errors), encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nSUMMARY", json.dumps(summary, ensure_ascii=False))
    print("ERROR_COUNT", len(errors))
    print("OUTPUT_DIR", out_dir)


if __name__ == "__main__":
    main()
