#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 首笔建仓事件研究（只读）。

研究问题：公开 KOL 对某 Mint 的历史首笔明显买入出现后，延迟 1/2/3/5/10 秒
跟入，价格路径是否仍有正收益窗口；同时观察首买后的非 KOL 跟单压力。

口径：
- pnl/token-list 只用于发现近期活跃 Mint，不使用网页/PnL 的交易次数。
- trading-history/filter-list + userAddressList=[KOL] 恢复 KOL×Mint 历史成交。
- userAddressList=[] 恢复事件附近全市场逐笔成交，统计跟单压力。
- 价格路径优先使用 OKX 1 秒 K 线；若 K 线窗口不可用，仅在逐笔窗口完整时回退逐笔价格。
- 本研究计算的是价格层面的毛收益，不包含实际跟单滑点、Gas、税费和 MEV。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PNL_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/pnl/token-list"
TRADE_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list"
KLINE_URL = "https://web3.okx.com/priapi/v5/dex/token/market/dex-token-hlc-candles"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DELAYS_S = (1, 2, 3, 5, 10)
HORIZONS_S = (5, 10, 30, 60, 300)
PRESSURE_WINDOWS_S = (1, 3, 5, 10)


def fnum(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def inum(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def tx_hash(row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not row:
        return None
    for key in ("txHash", "transactionHash", "transaction_hash", "hash"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    referer: str,
    retries: int = 3,
    timeout: int = 15,
) -> Tuple[int, Dict[str, Any]]:
    headers = {"User-Agent": UA, "Accept": "application/json", "Referer": referer}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


def fetch_recent_mints(wallet: str, chain_id: str, limit: int) -> List[Dict[str, Any]]:
    params = {
        "walletAddress": wallet,
        "chainId": chain_id,
        "isAsc": "false",
        "sortType": "1",
        "offset": "0",
        "limit": str(limit),
        "filterRisk": "true",
        "filterSmallBalance": "false",
        "t": str(int(time.time() * 1000)),
    }
    status, body = request_json(
        PNL_URL + "?" + urllib.parse.urlencode(params),
        referer="https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
    )
    if status != 200 or str(body.get("code")) != "0":
        raise RuntimeError(f"pnl failed status={status} code={body.get('code')} msg={body.get('msg')}")
    return ((body.get("data") or {}).get("tokenList") or [])


def trade_payload(chain_id: str, mint: str, users: List[str], data_id: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "desc": True,
        "orderBy": "timestamp",
        "limit": 30,
        "tradingHistoryFilter": {
            "chainId": chain_id,
            "tokenContractAddress": mint,
            "type": "0",
            "currentUserWalletAddress": "",
            "userAddressList": users,
            "volumeMin": "",
            "volumeMax": "",
            "priceMin": "",
            "priceMax": "",
            "amountMin": "",
            "amountMax": "",
        },
    }
    if data_id:
        out["dataId"] = data_id
    return out


def fetch_trade_page(chain_id: str, mint: str, users: List[str], data_id: Optional[str]) -> Dict[str, Any]:
    status, body = request_json(
        TRADE_URL + "?t=" + str(int(time.time() * 1000)),
        method="POST",
        payload=trade_payload(chain_id, mint, users, data_id),
        referer="https://web3.okx.com/zh-hans/market/dex",
    )
    if status != 200 or str(body.get("code")) != "0":
        raise RuntimeError(
            f"trade history failed status={status} code={body.get('code')} msg={body.get('msg')}"
        )
    return body.get("data") or {}


def dedup_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        key = r.get("id") or (
            r.get("timestamp"), r.get("userAddress"), r.get("isBuy"), r.get("volume"), r.get("price")
        )
        out[key] = r
    return list(out.values())


def fetch_kol_history(wallet: str, chain_id: str, mint: str, max_rows: int) -> Tuple[List[Dict[str, Any]], bool]:
    rows: List[Dict[str, Any]] = []
    data_id: Optional[str] = None
    complete = False
    pages = 0
    while len(rows) < max_rows and pages < 200:
        data = fetch_trade_page(chain_id, mint, [wallet], data_id)
        page = data.get("list") or []
        if not page:
            complete = True
            break
        rows.extend(page)
        pages += 1
        next_id = page[-1].get("id")
        if str(data.get("hasMore", "0")) != "1":
            complete = True
            break
        if not next_id or next_id == data_id:
            break
        data_id = next_id
        time.sleep(0.03)
    return dedup_rows(rows)[:max_rows], complete


def fetch_market_window(
    chain_id: str,
    mint: str,
    start_ms: int,
    end_ms: int,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], bool, int]:
    rows: List[Dict[str, Any]] = []
    data_id: Optional[str] = None
    pages = 0
    covered_start = False
    while pages < max_pages:
        data = fetch_trade_page(chain_id, mint, [], data_id)
        page = data.get("list") or []
        if not page:
            break
        pages += 1
        oldest = min(inum(r.get("timestamp")) for r in page)
        for r in page:
            ts = inum(r.get("timestamp"))
            if start_ms <= ts <= end_ms:
                rows.append(r)
        if oldest <= start_ms:
            covered_start = True
            break
        next_id = page[-1].get("id")
        if str(data.get("hasMore", "0")) != "1" or not next_id or next_id == data_id:
            break
        data_id = next_id
        time.sleep(0.02)
    rows = dedup_rows(rows)
    rows.sort(key=lambda r: inum(r.get("timestamp")))
    return rows, covered_start, pages


def fetch_candles(chain_id: str, mint: str, start_ms: int, end_ms: int) -> Tuple[List[Dict[str, float]], bool]:
    """请求覆盖事件窗口的 1 秒 K 线；OKX after 按向过去分页口径使用。"""
    limit = min(1000, max(420, int((end_ms - start_ms) / 1000) + 60))
    params = {
        "chainId": chain_id,
        "address": mint,
        "after": str(end_ms + 2000),
        "bar": "1s",
        "limit": str(limit),
        "t": str(int(time.time() * 1000)),
    }
    status, body = request_json(
        KLINE_URL + "?" + urllib.parse.urlencode(params),
        referer=f"https://web3.okx.com/zh-hans/token/{chain_id}/{mint}",
    )
    if status != 200 or str(body.get("code")) != "0":
        return [], False
    raw = body.get("data") or []
    rows: List[Dict[str, float]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 5:
            continue
        ts = inum(item[0])
        if start_ms <= ts <= end_ms + 2000:
            rows.append({
                "timestamp": float(ts),
                "open": fnum(item[1]),
                "high": fnum(item[2]),
                "low": fnum(item[3]),
                "close": fnum(item[4]),
            })
    rows.sort(key=lambda r: int(r["timestamp"]))
    if not rows:
        return [], False
    min_ts = int(rows[0]["timestamp"])
    max_ts = int(rows[-1]["timestamp"])
    covered = min_ts <= start_ms + 5000 and max_ts >= end_ms - 5000
    return rows, covered


def is_buy(r: Dict[str, Any]) -> bool:
    return str(r.get("isBuy")) == "1"


def row_price(r: Optional[Dict[str, Any]]) -> Optional[float]:
    if not r:
        return None
    p = fnum(r.get("price"))
    return p if p > 0 else None


def first_trade_after(rows: List[Dict[str, Any]], target_ms: int, wait_ms: int = 5000) -> Optional[Dict[str, Any]]:
    end = target_ms + wait_ms
    for r in rows:
        ts = inum(r.get("timestamp"))
        if target_ms <= ts <= end and row_price(r):
            return r
        if ts > end:
            break
    return None


def candle_price(rows: List[Dict[str, float]], target_ms: int, tolerance_ms: int = 5000) -> Optional[float]:
    best: Optional[Tuple[int, float]] = None
    for r in rows:
        ts = int(r["timestamp"])
        d = abs(ts - target_ms)
        if d <= tolerance_ms and r["close"] > 0 and (best is None or d < best[0]):
            best = (d, r["close"])
    return best[1] if best else None


def bps(entry: Optional[float], exit_: Optional[float]) -> Optional[float]:
    if not entry or not exit_ or entry <= 0:
        return None
    return (exit_ / entry - 1.0) * 10000.0


def pressure(rows: List[Dict[str, Any]], wallet: str, t0: int, sec: int) -> Dict[str, Any]:
    end = t0 + sec * 1000
    xs = [
        r for r in rows
        if t0 < inum(r.get("timestamp")) <= end
        and str(r.get("userAddress") or "").lower() != wallet.lower()
    ]
    buys = [r for r in xs if is_buy(r)]
    sells = [r for r in xs if not is_buy(r)]
    buy_usd = sum(fnum(r.get("volume")) for r in buys)
    sell_usd = sum(fnum(r.get("volume")) for r in sells)
    buyers = {str(r.get("userAddress") or "").lower() for r in buys if r.get("userAddress")}
    return {
        f"followers_{sec}s_buy_trades": len(buys),
        f"followers_{sec}s_unique_buyers": len(buyers),
        f"followers_{sec}s_buy_usd": round(buy_usd, 6),
        f"followers_{sec}s_sell_usd": round(sell_usd, 6),
        f"followers_{sec}s_net_buy_usd": round(buy_usd - sell_usd, 6),
    }


def buy_usd(rows: List[Dict[str, Any]], wallet: str, start: int, end: int) -> float:
    return sum(
        fnum(r.get("volume")) for r in rows
        if start <= inum(r.get("timestamp")) < end
        and is_buy(r)
        and str(r.get("userAddress") or "").lower() != wallet.lower()
    )


def analyze_event(
    kol: Dict[str, Any],
    pnl_item: Dict[str, Any],
    history: List[Dict[str, Any]],
    history_complete: bool,
    args: argparse.Namespace,
) -> Optional[Dict[str, Any]]:
    wallet = kol["address"]
    mint = pnl_item.get("tokenContractAddress")
    chain_id = kol["chain_id"]
    chron = sorted(history, key=lambda r: inum(r.get("timestamp")))
    buys = [r for r in chron if is_buy(r)]
    if not buys:
        return None
    first = buys[0]
    first_usd = fnum(first.get("volume"))
    t0 = inum(first.get("timestamp"))
    if first_usd < args.min_first_buy_usd:
        return None
    age_h = (int(time.time() * 1000) - t0) / 3_600_000 if t0 else math.inf
    if age_h < 0 or age_h > args.max_event_age_hours:
        return None

    later = [r for r in chron if inum(r.get("timestamp")) > t0]
    sells = [r for r in later if not is_buy(r)]
    first_sell = sells[0] if sells else None
    sell_ts = inum(first_sell.get("timestamp")) if first_sell else None
    sell_delay = (sell_ts - t0) / 1000 if sell_ts else None
    before_sell_buys = [
        r for r in later if is_buy(r) and (sell_ts is None or inum(r.get("timestamp")) < sell_ts)
    ]
    probes = [r for r in before_sell_buys if fnum(r.get("volume")) <= first_usd * 0.10]
    probe_hashes = [tx_hash(r) for r in probes if tx_hash(r)]

    start = t0 - args.pre_window_seconds * 1000
    end = t0 + args.post_window_seconds * 1000
    market, market_complete, market_pages = fetch_market_window(
        chain_id, mint, start, end, args.max_market_pages
    )
    try:
        candles, candle_complete = fetch_candles(chain_id, mint, start, end)
    except Exception:  # noqa: BLE001
        candles, candle_complete = [], False

    price_source = "kline_1s" if candle_complete else ("market_trades" if market_complete else None)
    price_complete = price_source is not None

    event: Dict[str, Any] = {
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
        "first_buy_price": row_price(first),
        "first_buy_id": first.get("id"),
        "first_buy_tx_hash": tx_hash(first),
        "history_rows": len(chron),
        "history_complete": history_complete,
        "kol_buy_rows": len(buys),
        "kol_sell_rows": len([r for r in chron if not is_buy(r)]),
        "first_sell_delay_s": round(sell_delay, 3) if sell_delay is not None else None,
        "first_sell_usd": round(fnum(first_sell.get("volume")), 6) if first_sell else None,
        "first_sell_price": row_price(first_sell),
        "first_sell_tx_hash": tx_hash(first_sell),
        "pre_first_sell_extra_buys": len(before_sell_buys),
        "pre_first_sell_small_probe_buys": len(probes),
        "small_probe_buy_usd": [round(fnum(r.get("volume")), 6) for r in probes[:30]],
        "small_probe_tx_hashes": probe_hashes[:30],
        "small_probe_unique_tx_hashes": len(set(probe_hashes)) if probe_hashes else None,
        "market_rows": len(market),
        "market_window_complete": market_complete,
        "market_pages": market_pages,
        "candle_rows": len(candles),
        "candle_window_complete": candle_complete,
        "price_window_complete": price_complete,
        "price_source": price_source,
    }

    if market_complete:
        pre5 = buy_usd(market, wallet, t0 - 5000, t0)
        post5 = buy_usd(market, wallet, t0, t0 + 5000)
        event["followers_pre5s_buy_usd"] = round(pre5, 6)
        event["followers_post5s_buy_usd"] = round(post5, 6)
        event["followers_post5_vs_pre5_ratio"] = round(post5 / pre5, 6) if pre5 > 0 else None
        for sec in PRESSURE_WINDOWS_S:
            event.update(pressure(market, wallet, t0, sec))
    else:
        event["followers_pre5s_buy_usd"] = None
        event["followers_post5s_buy_usd"] = None
        event["followers_post5_vs_pre5_ratio"] = None
        for sec in PRESSURE_WINDOWS_S:
            for suffix in ("buy_trades", "unique_buyers", "buy_usd", "sell_usd", "net_buy_usd"):
                event[f"followers_{sec}s_{suffix}"] = None

    for delay in DELAYS_S:
        target = t0 + delay * 1000
        if candle_complete:
            entry_price = candle_price(candles, target)
            entry_ts = target if entry_price else None
        elif market_complete:
            tr = first_trade_after(market, target)
            entry_price = row_price(tr)
            entry_ts = inum(tr.get("timestamp")) if tr else None
        else:
            entry_price = None
            entry_ts = None
        event[f"entry_{delay}s_price"] = entry_price
        event[f"entry_{delay}s_actual_delay_ms"] = (entry_ts - t0) if entry_ts else None

        if entry_price and price_complete:
            if candle_complete:
                future = [
                    r for r in candles
                    if target <= int(r["timestamp"]) <= end and r["high"] > 0 and r["low"] > 0
                ]
                max_p = max((r["high"] for r in future), default=None)
                min_p = min((r["low"] for r in future), default=None)
            else:
                ps = [
                    row_price(r) for r in market
                    if target <= inum(r.get("timestamp")) <= end and row_price(r)
                ]
                max_p = max(ps) if ps else None
                min_p = min(ps) if ps else None
            mfe = bps(entry_price, max_p)
            mae = bps(entry_price, min_p)
            event[f"entry_{delay}s_mfe_bps"] = round(mfe, 3) if mfe is not None else None
            event[f"entry_{delay}s_mae_bps"] = round(mae, 3) if mae is not None else None
        else:
            event[f"entry_{delay}s_mfe_bps"] = None
            event[f"entry_{delay}s_mae_bps"] = None

        sell_ret = bps(entry_price, row_price(first_sell))
        event[f"entry_{delay}s_to_kol_first_sell_bps"] = round(sell_ret, 3) if sell_ret is not None else None

        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            mark_target = t0 + horizon * 1000
            if candle_complete:
                mark = candle_price(candles, mark_target)
            elif market_complete:
                mark = row_price(first_trade_after(market, mark_target))
            else:
                mark = None
            ret = bps(entry_price, mark)
            event[f"entry_{delay}s_to_{horizon}s_bps"] = round(ret, 3) if ret is not None else None
    return event


def med(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 3) if values else None


def mean(values: List[float]) -> Optional[float]:
    return round(statistics.fmean(values), 3) if values else None


def metric(values: List[float]) -> Dict[str, Any]:
    return {
        "n": len(values),
        "median_bps": med(values),
        "mean_bps": mean(values),
        "win_rate": round(sum(v > 0 for v in values) / len(values), 4) if values else None,
    }


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [e for e in events if e.get("price_window_complete")]
    follower_valid = [e for e in events if e.get("market_window_complete")]
    out: Dict[str, Any] = {
        "event_count": len(events),
        "price_valid_event_count": len(valid),
        "follower_valid_event_count": len(follower_valid),
        "by_kol": {},
        "delay_metrics": {},
        "follower_pressure": {},
    }
    by: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"events": 0, "price_valid": 0})
    for e in events:
        by[e["kol"]]["events"] += 1
        if e.get("price_window_complete"):
            by[e["kol"]]["price_valid"] += 1
    for name, item in by.items():
        es = [e for e in valid if e["kol"] == name]
        r10 = [float(e["entry_1s_to_10s_bps"]) for e in es if e.get("entry_1s_to_10s_bps") is not None]
        r30 = [float(e["entry_1s_to_30s_bps"]) for e in es if e.get("entry_1s_to_30s_bps") is not None]
        sell = [float(e["entry_1s_to_kol_first_sell_bps"]) for e in es if e.get("entry_1s_to_kol_first_sell_bps") is not None]
        item.update({
            "entry1_to10_median_bps": med(r10),
            "entry1_to30_median_bps": med(r30),
            "entry1_to_first_sell_median_bps": med(sell),
        })
        out["by_kol"][name] = item

    for delay in DELAYS_S:
        d: Dict[str, Any] = {}
        sell_key = f"entry_{delay}s_to_kol_first_sell_bps"
        d["to_kol_first_sell"] = metric([float(e[sell_key]) for e in valid if e.get(sell_key) is not None])
        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            key = f"entry_{delay}s_to_{horizon}s_bps"
            d[f"to_{horizon}s"] = metric([float(e[key]) for e in valid if e.get(key) is not None])
        d["median_mfe_bps"] = med([float(e[f"entry_{delay}s_mfe_bps"]) for e in valid if e.get(f"entry_{delay}s_mfe_bps") is not None])
        d["median_mae_bps"] = med([float(e[f"entry_{delay}s_mae_bps"]) for e in valid if e.get(f"entry_{delay}s_mae_bps") is not None])
        out["delay_metrics"][str(delay)] = d

    for sec in PRESSURE_WINDOWS_S:
        unique = [float(e[f"followers_{sec}s_unique_buyers"]) for e in follower_valid if e.get(f"followers_{sec}s_unique_buyers") is not None]
        usd = [float(e[f"followers_{sec}s_buy_usd"]) for e in follower_valid if e.get(f"followers_{sec}s_buy_usd") is not None]
        out["follower_pressure"][str(sec)] = {
            "n": len(unique),
            "median_unique_buyers": med(unique),
            "median_buy_usd": med(usd),
        }
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--watchlist", default="research/kol_trading_behavior/kol_watchlist.json")
    p.add_argument("--output-dir", default="research/kol_trading_behavior/out_first_buy_study")
    p.add_argument("--chains", default="56")
    p.add_argument("--tiers", default="primary")
    p.add_argument("--recent-mints", type=int, default=5)
    p.add_argument("--max-kol-history-rows", type=int, default=300)
    p.add_argument("--min-first-buy-usd", type=float, default=100.0)
    p.add_argument("--max-event-age-hours", type=float, default=48.0)
    p.add_argument("--pre-window-seconds", type=int, default=5)
    p.add_argument("--post-window-seconds", type=int, default=300)
    p.add_argument("--max-market-pages", type=int, default=160)
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
    print("KOL_COUNT", len(kols))

    for kol in kols:
        print(f"\n=== {kol['name']} {kol['address']} chain={kol['chain_id']} ===")
        try:
            mints = fetch_recent_mints(kol["address"], kol["chain_id"], a.recent_mints)
        except Exception as exc:  # noqa: BLE001
            errors.append({"kol": kol["name"], "stage": "recent_mints", "error": str(exc)})
            print("ERROR recent_mints", exc)
            continue
        for item in mints:
            mint = item.get("tokenContractAddress")
            if not mint:
                continue
            try:
                history, complete = fetch_kol_history(kol["address"], kol["chain_id"], mint, a.max_kol_history_rows)
                if not complete:
                    print("SKIP incomplete_history", kol["name"], item.get("tokenSymbol"), mint, len(history))
                    continue
                event = analyze_event(kol, item, history, complete, a)
                if event:
                    events.append(event)
                    print("EVENT", json.dumps({
                        "kol": event["kol"],
                        "symbol": event["symbol"],
                        "first_buy_usd": event["first_buy_usd"],
                        "sell_delay_s": event["first_sell_delay_s"],
                        "probe_buys": event["pre_first_sell_small_probe_buys"],
                        "probe_unique_tx": event["small_probe_unique_tx_hashes"],
                        "price_source": event["price_source"],
                        "market_complete": event["market_window_complete"],
                        "candle_complete": event["candle_window_complete"],
                    }, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "kol": kol["name"],
                    "mint": mint,
                    "symbol": item.get("tokenSymbol"),
                    "stage": "event",
                    "error": str(exc),
                })
                print("ERROR event", kol["name"], item.get("tokenSymbol"), exc)

    summary = summarize(events)
    (out_dir / "events.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in events), encoding="utf-8"
    )
    (out_dir / "errors.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in errors), encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nSUMMARY", json.dumps(summary, ensure_ascii=False))
    print("ERROR_COUNT", len(errors))
    print("OUTPUT_DIR", out_dir)


if __name__ == "__main__":
    main()
