#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOL 首笔建仓事件研究（只读）。

研究问题：公开 KOL 对某 Mint 的历史首笔明显买入出现后，延迟 1/2/3/5/10 秒
跟入，价格路径是否仍有正收益窗口；同时比较 KOL 自身收益、跟单压力和跟随者收益。

数据口径：
- pnl/token-list 只用于发现近期活跃 Mint，并保留其 PnL 字段作为参考；不使用网页/PnL 的交易次数。
- trading-history/filter-list + userAddressList=[KOL] 恢复 KOL×Mint 历史成交。
- 全市场逐笔使用 startTime/endTime + dataId 时间戳直接定位事件窗口。
- OKX 实测 trading-history 单页有效上限为 100；同一 cursor 链顺序分页，不并发同一 Mint 的页。
- 不同 Mint/Event 可以并行处理。
- 价格路径优先使用 OKX 1 秒 K 线；若 K 线窗口不可用，仅在逐笔窗口完整时回退逐笔价格。
- KOL 首买->首卖收益按逐笔实际成交价计算。
- 整轮 KOL 收益仅在按成交数量估算剩余仓位 <=1% 时，将 SELL USD - BUY USD 视为已完成轮次毛收益。
- 所有收益均未扣除跟随者自己的 Gas、买卖税、滑点、价格冲击和 MEV。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PNL_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/pnl/token-list"
TRADE_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list"
KLINE_URL = "https://web3.okx.com/priapi/v5/dex/token/market/dex-token-hlc-candles"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TRADE_PAGE_LIMIT = 100
DELAYS_S = (1, 2, 3, 5, 10)
HORIZONS_S = (5, 10, 20, 30, 60, 300)
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
    url = row.get("txHashUrl")
    if url:
        return str(url).rstrip("/").split("/")[-1]
    return None


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    referer: str,
    retries: int = 4,
    timeout: int = 20,
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
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                break
        except Exception as exc:  # noqa: BLE001 - standalone research script
            last = exc
        if attempt + 1 < retries:
            time.sleep(0.6 * (2 ** attempt))
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


def trade_payload(
    chain_id: str,
    mint: str,
    users: List[str],
    data_id: Optional[str],
    *,
    limit: int = TRADE_PAGE_LIMIT,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {
        "chainId": chain_id,
        "tokenContractAddress": mint,
        "type": "0",
        "currentUserWalletAddress": "",
        "userAddressList": users,
        "volumeMin": "", "volumeMax": "",
        "priceMin": "", "priceMax": "",
        "amountMin": "", "amountMax": "",
    }
    # GitHub Runner 2026-08-26 实测：这两个字段会在服务端按时间过滤。
    if start_ms is not None:
        filters["startTime"] = int(start_ms)
    if end_ms is not None:
        filters["endTime"] = int(end_ms)

    out: Dict[str, Any] = {
        "desc": True,
        "orderBy": "timestamp",
        "limit": max(1, min(int(limit), TRADE_PAGE_LIMIT)),
        "tradingHistoryFilter": filters,
    }
    # 实测 dataId 可直接传毫秒时间戳；后续页继续用服务端返回的复合 id。
    if data_id is not None:
        out["dataId"] = str(data_id)
    return out


def fetch_trade_page(
    chain_id: str,
    mint: str,
    users: List[str],
    data_id: Optional[str],
    *,
    limit: int = TRADE_PAGE_LIMIT,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dict[str, Any]:
    status, body = request_json(
        TRADE_URL + "?t=" + str(int(time.time() * 1000)),
        method="POST",
        payload=trade_payload(
            chain_id, mint, users, data_id,
            limit=limit, start_ms=start_ms, end_ms=end_ms,
        ),
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


def fetch_kol_history(
    wallet: str,
    chain_id: str,
    mint: str,
    max_rows: int,
) -> Tuple[List[Dict[str, Any]], bool, int]:
    rows: List[Dict[str, Any]] = []
    data_id: Optional[str] = None
    complete = False
    pages = 0
    max_pages = max(1, math.ceil(max_rows / TRADE_PAGE_LIMIT) + 1)

    while len(rows) < max_rows and pages < max_pages:
        data = fetch_trade_page(chain_id, mint, [wallet], data_id, limit=TRADE_PAGE_LIMIT)
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
        data_id = str(next_id)
        time.sleep(0.01)

    rows = dedup_rows(rows)
    rows.sort(key=lambda r: inum(r.get("timestamp")))
    return rows[:max_rows], complete, pages


def fetch_market_window(
    chain_id: str,
    mint: str,
    start_ms: int,
    end_ms: int,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], bool, int]:
    """直接定位 [start_ms, end_ms]；不再从最新成交一路向过去扫描。"""
    rows: List[Dict[str, Any]] = []
    # 实测裸毫秒时间戳可作为 dataId，第一请求直接跳到 end_ms 附近。
    data_id: Optional[str] = str(end_ms)
    pages = 0
    complete = False

    while pages < max_pages:
        data = fetch_trade_page(
            chain_id,
            mint,
            [],
            data_id,
            limit=TRADE_PAGE_LIMIT,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        page = data.get("list") or []
        pages += 1

        # 时间过滤后的空页代表窗口内没有更多成交，本窗口完整。
        if not page:
            complete = True
            break

        for r in page:
            ts = inum(r.get("timestamp"))
            if start_ms <= ts <= end_ms:
                rows.append(r)

        oldest = min(inum(r.get("timestamp")) for r in page)
        if oldest <= start_ms:
            complete = True
            break

        next_id = page[-1].get("id")
        has_more = str(data.get("hasMore", "0")) == "1"
        if not has_more:
            # 因为 startTime/endTime 已在服务端过滤，hasMore=0 即窗口已取完。
            complete = True
            break
        if not next_id or str(next_id) == str(data_id):
            break
        data_id = str(next_id)
        time.sleep(0.005)

    rows = dedup_rows(rows)
    rows.sort(key=lambda r: inum(r.get("timestamp")))
    return rows, complete, pages


def fetch_candles(
    chain_id: str,
    mint: str,
    start_ms: int,
    end_ms: int,
) -> Tuple[List[Dict[str, float]], bool]:
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

    rows: List[Dict[str, float]] = []
    for item in body.get("data") or []:
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
    covered = (
        int(rows[0]["timestamp"]) <= start_ms + 5000
        and int(rows[-1]["timestamp"]) >= end_ms - 5000
    )
    return rows, covered


def is_buy(r: Dict[str, Any]) -> bool:
    return str(r.get("isBuy")) == "1"


def row_price(r: Optional[Dict[str, Any]]) -> Optional[float]:
    if not r:
        return None
    p = fnum(r.get("price"))
    return p if p > 0 else None


def token_qty(r: Dict[str, Any]) -> float:
    for key in ("amount", "tokenAmount", "tokenQuantity", "quantity"):
        q = fnum(r.get(key))
        if q > 0:
            return q
    p = row_price(r)
    v = fnum(r.get("volume"))
    return (v / p) if p and p > 0 and v > 0 else 0.0


def first_trade_after(
    rows: List[Dict[str, Any]],
    target_ms: int,
    wait_ms: int = 5000,
) -> Optional[Dict[str, Any]]:
    end = target_ms + wait_ms
    for r in rows:
        ts = inum(r.get("timestamp"))
        if target_ms <= ts <= end and row_price(r):
            return r
        if ts > end:
            break
    return None


def candle_price(
    rows: List[Dict[str, float]],
    target_ms: int,
    tolerance_ms: int = 5000,
) -> Optional[float]:
    best: Optional[Tuple[int, float]] = None
    for r in rows:
        d = abs(int(r["timestamp"]) - target_ms)
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
    buy_value = sum(fnum(r.get("volume")) for r in buys)
    sell_value = sum(fnum(r.get("volume")) for r in sells)
    buyers = {str(r.get("userAddress") or "").lower() for r in buys if r.get("userAddress")}
    return {
        f"followers_{sec}s_buy_trades": len(buys),
        f"followers_{sec}s_unique_buyers": len(buyers),
        f"followers_{sec}s_buy_usd": round(buy_value, 6),
        f"followers_{sec}s_sell_usd": round(sell_value, 6),
        f"followers_{sec}s_net_buy_usd": round(buy_value - sell_value, 6),
    }


def non_kol_buy_usd(rows: List[Dict[str, Any]], wallet: str, start: int, end: int) -> float:
    return sum(
        fnum(r.get("volume")) for r in rows
        if start <= inum(r.get("timestamp")) < end
        and is_buy(r)
        and str(r.get("userAddress") or "").lower() != wallet.lower()
    )


def kol_round_metrics(chron: List[Dict[str, Any]]) -> Dict[str, Any]:
    buys = [r for r in chron if is_buy(r)]
    sells = [r for r in chron if not is_buy(r)]
    buy_usd = sum(fnum(r.get("volume")) for r in buys)
    sell_usd = sum(fnum(r.get("volume")) for r in sells)
    buy_qty = sum(token_qty(r) for r in buys)
    sell_qty = sum(token_qty(r) for r in sells)
    remaining_qty = buy_qty - sell_qty
    remaining_pct = remaining_qty / buy_qty if buy_qty > 0 else None
    closed = remaining_pct is not None and abs(remaining_pct) <= 0.01
    cashflow = sell_usd - buy_usd
    roi_bps = (cashflow / buy_usd * 10000.0) if closed and buy_usd > 0 else None
    buy_vwap = (buy_usd / buy_qty) if buy_qty > 0 else None
    sell_vwap = (sell_usd / sell_qty) if sell_qty > 0 else None
    return {
        "kol_total_buy_usd": round(buy_usd, 6),
        "kol_total_sell_usd": round(sell_usd, 6),
        "kol_total_buy_qty": buy_qty,
        "kol_total_sell_qty": sell_qty,
        "kol_remaining_qty_est": remaining_qty,
        "kol_remaining_pct_est": round(remaining_pct, 6) if remaining_pct is not None else None,
        "kol_round_closed_est": closed,
        "kol_round_cashflow_usd": round(cashflow, 6),
        "kol_round_realized_pnl_usd": round(cashflow, 6) if closed else None,
        "kol_round_realized_roi_bps": round(roi_bps, 3) if roi_bps is not None else None,
        "kol_buy_vwap": buy_vwap,
        "kol_sell_vwap": sell_vwap,
    }


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
    later_sells = [r for r in later if not is_buy(r)]
    first_sell = later_sells[0] if later_sells else None
    sell_ts = inum(first_sell.get("timestamp")) if first_sell else None
    sell_delay = (sell_ts - t0) / 1000 if sell_ts else None
    before_sell_buys = [
        r for r in later
        if is_buy(r) and (sell_ts is None or inum(r.get("timestamp")) < sell_ts)
    ]
    probes = [r for r in before_sell_buys if fnum(r.get("volume")) <= first_usd * 0.10]
    probe_hashes = [tx_hash(r) for r in probes if tx_hash(r)]

    start = t0 - args.pre_window_seconds * 1000
    end = t0 + args.post_window_seconds * 1000

    candle_t0 = time.perf_counter()
    try:
        candles, candle_complete = fetch_candles(chain_id, mint, start, end)
    except Exception:  # noqa: BLE001
        candles, candle_complete = [], False
    candle_fetch_s = time.perf_counter() - candle_t0

    market: List[Dict[str, Any]] = []
    market_complete = False
    market_pages = 0
    market_fetch_s: Optional[float] = None
    if age_h <= args.max_follower_event_age_hours:
        market_t0 = time.perf_counter()
        try:
            market, market_complete, market_pages = fetch_market_window(
                chain_id, mint, start, end, args.max_market_pages
            )
        except Exception:  # noqa: BLE001
            market, market_complete, market_pages = [], False, 0
        market_fetch_s = time.perf_counter() - market_t0

    price_source = "kline_1s" if candle_complete else ("market_trades" if market_complete else None)
    price_complete = price_source is not None
    first_sell_ret = bps(row_price(first), row_price(first_sell))

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
        "history_pages": history_pages,
        "kol_buy_rows": len(buys),
        "kol_sell_rows": len([r for r in chron if not is_buy(r)]),
        "first_sell_delay_s": round(sell_delay, 3) if sell_delay is not None else None,
        "first_sell_usd": round(fnum(first_sell.get("volume")), 6) if first_sell else None,
        "first_sell_price": row_price(first_sell),
        "first_sell_tx_hash": tx_hash(first_sell),
        "kol_first_buy_to_first_sell_bps": round(first_sell_ret, 3) if first_sell_ret is not None else None,
        "pre_first_sell_extra_buys": len(before_sell_buys),
        "pre_first_sell_small_probe_buys": len(probes),
        "small_probe_buy_usd": [round(fnum(r.get("volume")), 6) for r in probes[:50]],
        "small_probe_tx_hashes": probe_hashes[:50],
        "small_probe_unique_tx_hashes": len(set(probe_hashes)) if probe_hashes else None,
        "market_rows": len(market),
        "market_window_complete": market_complete,
        "market_pages": market_pages,
        "market_fetch_seconds": round(market_fetch_s, 4) if market_fetch_s is not None else None,
        "candle_rows": len(candles),
        "candle_window_complete": candle_complete,
        "candle_fetch_seconds": round(candle_fetch_s, 4),
        "price_window_complete": price_complete,
        "price_source": price_source,
        "pnl_reference_total_pnl_usd": fnum(pnl_item.get("totalPnl")),
        "pnl_reference_total_pnl_pct": fnum(pnl_item.get("totalPnlPercentage")),
        "pnl_reference_realized_pnl_usd": fnum(pnl_item.get("realizedPnl")),
        "pnl_reference_unrealized_pnl_usd": fnum(pnl_item.get("unrealizedPnl")),
        "pnl_reference_balance": fnum(pnl_item.get("balance")),
    }
    event.update(kol_round_metrics(chron))

    if market_complete:
        pre5 = non_kol_buy_usd(market, wallet, t0 - 5000, t0)
        post5 = non_kol_buy_usd(market, wallet, t0, t0 + 5000)
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
        penalty = bps(row_price(first), entry_price)
        event[f"entry_{delay}s_vs_kol_entry_bps"] = round(penalty, 3) if penalty is not None else None

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
        if sell_ret is not None and first_sell_ret is not None:
            event[f"entry_{delay}s_first_sell_edge_vs_kol_bps"] = round(sell_ret - first_sell_ret, 3)
        else:
            event[f"entry_{delay}s_first_sell_edge_vs_kol_bps"] = None

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


def process_candidate(
    kol: Dict[str, Any],
    item: Dict[str, Any],
    args: argparse.Namespace,
) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    mint = item.get("tokenContractAddress")
    if not mint:
        return "skip", None, None
    try:
        history, complete, pages = fetch_kol_history(
            kol["address"], kol["chain_id"], mint, args.max_kol_history_rows
        )
        if not complete:
            return "skip", None, {
                "kol": kol["name"], "mint": mint, "symbol": item.get("tokenSymbol"),
                "stage": "incomplete_history", "history_rows": len(history), "history_pages": pages,
            }
        event = analyze_event(kol, item, history, complete, pages, args)
        return "event" if event else "skip", event, None
    except Exception as exc:  # noqa: BLE001
        return "error", None, {
            "kol": kol["name"], "mint": mint, "symbol": item.get("tokenSymbol"),
            "stage": "event", "error": str(exc),
        }


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


def bucket_metric(events: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    r10 = [float(e["entry_1s_to_10s_bps"]) for e in events if e.get("entry_1s_to_10s_bps") is not None]
    r30 = [float(e["entry_1s_to_30s_bps"]) for e in events if e.get("entry_1s_to_30s_bps") is not None]
    return {
        "events": len(events),
        "key": key,
        "entry1_to10": metric(r10),
        "entry1_to30": metric(r30),
    }


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [e for e in events if e.get("price_window_complete")]
    follower_valid = [e for e in events if e.get("market_window_complete")]
    closed = [e for e in events if e.get("kol_round_closed_est")]
    out: Dict[str, Any] = {
        "event_count": len(events),
        "price_valid_event_count": len(valid),
        "follower_valid_event_count": len(follower_valid),
        "kol_closed_round_count": len(closed),
        "fetch_performance": {
            "market_fetch_median_seconds": med([
                float(e["market_fetch_seconds"]) for e in events
                if e.get("market_fetch_seconds") is not None
            ]),
            "market_pages_median": med([
                float(e["market_pages"]) for e in events
                if e.get("market_fetch_seconds") is not None
            ]),
            "history_pages_median": med([float(e["history_pages"]) for e in events]),
        },
        "kol_own": {
            "first_buy_to_first_sell": metric([
                float(e["kol_first_buy_to_first_sell_bps"]) for e in events
                if e.get("kol_first_buy_to_first_sell_bps") is not None
            ]),
            "closed_round_roi": metric([
                float(e["kol_round_realized_roi_bps"]) for e in closed
                if e.get("kol_round_realized_roi_bps") is not None
            ]),
        },
        "by_kol": {},
        "delay_metrics": {},
        "follower_pressure": {},
        "condition_buckets": {},
    }

    names = sorted({e["kol"] for e in events})
    for name in names:
        all_es = [e for e in events if e["kol"] == name]
        es = [e for e in valid if e["kol"] == name]
        own = [float(e["kol_first_buy_to_first_sell_bps"]) for e in all_es if e.get("kol_first_buy_to_first_sell_bps") is not None]
        own_closed = [float(e["kol_round_realized_roi_bps"]) for e in all_es if e.get("kol_round_realized_roi_bps") is not None]
        r10 = [float(e["entry_1s_to_10s_bps"]) for e in es if e.get("entry_1s_to_10s_bps") is not None]
        r30 = [float(e["entry_1s_to_30s_bps"]) for e in es if e.get("entry_1s_to_30s_bps") is not None]
        sell = [float(e["entry_1s_to_kol_first_sell_bps"]) for e in es if e.get("entry_1s_to_kol_first_sell_bps") is not None]
        out["by_kol"][name] = {
            "events": len(all_es),
            "price_valid": len(es),
            "kol_first_buy_to_first_sell": metric(own),
            "kol_closed_round_roi": metric(own_closed),
            "entry1_to10": metric(r10),
            "entry1_to30": metric(r30),
            "entry1_to_first_sell": metric(sell),
        }

    for delay in DELAYS_S:
        d: Dict[str, Any] = {}
        penalty_key = f"entry_{delay}s_vs_kol_entry_bps"
        sell_key = f"entry_{delay}s_to_kol_first_sell_bps"
        d["entry_penalty_vs_kol"] = metric([
            float(e[penalty_key]) for e in valid if e.get(penalty_key) is not None
        ])
        d["to_kol_first_sell"] = metric([
            float(e[sell_key]) for e in valid if e.get(sell_key) is not None
        ])
        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            key = f"entry_{delay}s_to_{horizon}s_bps"
            d[f"to_{horizon}s"] = metric([
                float(e[key]) for e in valid if e.get(key) is not None
            ])
        d["median_mfe_bps"] = med([
            float(e[f"entry_{delay}s_mfe_bps"]) for e in valid
            if e.get(f"entry_{delay}s_mfe_bps") is not None
        ])
        d["median_mae_bps"] = med([
            float(e[f"entry_{delay}s_mae_bps"]) for e in valid
            if e.get(f"entry_{delay}s_mae_bps") is not None
        ])
        out["delay_metrics"][str(delay)] = d

    for sec in PRESSURE_WINDOWS_S:
        unique = [
            float(e[f"followers_{sec}s_unique_buyers"]) for e in follower_valid
            if e.get(f"followers_{sec}s_unique_buyers") is not None
        ]
        usd = [
            float(e[f"followers_{sec}s_buy_usd"]) for e in follower_valid
            if e.get(f"followers_{sec}s_buy_usd") is not None
        ]
        net = [
            float(e[f"followers_{sec}s_net_buy_usd"]) for e in follower_valid
            if e.get(f"followers_{sec}s_net_buy_usd") is not None
        ]
        out["follower_pressure"][str(sec)] = {
            "n": len(unique),
            "median_unique_buyers": med(unique),
            "median_buy_usd": med(usd),
            "median_net_buy_usd": med(net),
        }

    size_defs = [
        ("100_200", 100, 200),
        ("200_500", 200, 500),
        ("500_1000", 500, 1000),
        ("1000_plus", 1000, math.inf),
    ]
    out["condition_buckets"]["first_buy_usd"] = {
        label: bucket_metric([e for e in valid if lo <= fnum(e.get("first_buy_usd")) < hi], label)
        for label, lo, hi in size_defs
    }

    pressure_defs = [
        ("0_2", 0, 3),
        ("3_5", 3, 6),
        ("6_10", 6, 11),
        ("11_plus", 11, math.inf),
    ]
    out["condition_buckets"]["followers_3s_unique_buyers"] = {
        label: bucket_metric([
            e for e in follower_valid
            if e.get("price_window_complete")
            and lo <= fnum(e.get("followers_3s_unique_buyers")) < hi
        ], label)
        for label, lo, hi in pressure_defs
    }

    net_defs = [
        ("non_positive", -math.inf, 0.000001),
        ("0_200", 0.000001, 200),
        ("200_500", 200, 500),
        ("500_plus", 500, math.inf),
    ]
    out["condition_buckets"]["followers_5s_net_buy_usd"] = {
        label: bucket_metric([
            e for e in follower_valid
            if e.get("price_window_complete")
            and lo <= fnum(e.get("followers_5s_net_buy_usd")) < hi
        ], label)
        for label, lo, hi in net_defs
    }
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--watchlist", default="research/kol_trading_behavior/kol_watchlist.json")
    p.add_argument("--output-dir", default="research/kol_trading_behavior/out_first_buy_study")
    p.add_argument("--chains", default="56")
    p.add_argument("--tiers", default="primary")
    p.add_argument("--recent-mints", type=int, default=20)
    p.add_argument("--max-kol-history-rows", type=int, default=600)
    p.add_argument("--min-first-buy-usd", type=float, default=100.0)
    p.add_argument("--max-event-age-hours", type=float, default=168.0)
    p.add_argument("--max-follower-event-age-hours", type=float, default=72.0)
    p.add_argument("--pre-window-seconds", type=int, default=5)
    p.add_argument("--post-window-seconds", type=int, default=300)
    p.add_argument("--max-market-pages", type=int, default=30)
    p.add_argument("--workers", type=int, default=4)
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

    print("KOL_COUNT", len(kols))
    print("WORKERS", max(1, a.workers))
    print("TRADE_PAGE_LIMIT", TRADE_PAGE_LIMIT)

    # PnL 发现阶段请求量很小，顺序做；真正重的 Mint/Event 阶段并行。
    for kol in kols:
        print(f"DISCOVER {kol['name']} {kol['address']} chain={kol['chain_id']}")
        try:
            mints = fetch_recent_mints(kol["address"], kol["chain_id"], a.recent_mints)
        except Exception as exc:  # noqa: BLE001
            errors.append({"kol": kol["name"], "stage": "recent_mints", "error": str(exc)})
            print("ERROR recent_mints", kol["name"], exc)
            continue
        for item in mints:
            if item.get("tokenContractAddress"):
                candidates.append((kol, item))

    print("CANDIDATE_COUNT", len(candidates))
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        future_map = {
            pool.submit(process_candidate, kol, item, a): (kol, item)
            for kol, item in candidates
        }
        for future in as_completed(future_map):
            kol, item = future_map[future]
            try:
                status, event, err = future.result()
            except Exception as exc:  # defensive
                status, event, err = "error", None, {
                    "kol": kol["name"],
                    "mint": item.get("tokenContractAddress"),
                    "symbol": item.get("tokenSymbol"),
                    "stage": "future",
                    "error": str(exc),
                }

            if err:
                errors.append(err)
                if status == "error":
                    print("ERROR", json.dumps(err, ensure_ascii=False))
                else:
                    print("SKIP", json.dumps(err, ensure_ascii=False))
            if event:
                events.append(event)
                print("EVENT", json.dumps({
                    "kol": event["kol"],
                    "symbol": event["symbol"],
                    "first_buy_usd": event["first_buy_usd"],
                    "kol_first_sell_bps": event["kol_first_buy_to_first_sell_bps"],
                    "kol_round_closed": event["kol_round_closed_est"],
                    "kol_round_roi_bps": event["kol_round_realized_roi_bps"],
                    "sell_delay_s": event["first_sell_delay_s"],
                    "probe_buys": event["pre_first_sell_small_probe_buys"],
                    "history_pages": event["history_pages"],
                    "market_pages": event["market_pages"],
                    "market_fetch_s": event["market_fetch_seconds"],
                    "market_complete": event["market_window_complete"],
                    "price_source": event["price_source"],
                }, ensure_ascii=False))

    events.sort(key=lambda e: (e.get("kol") or "", inum(e.get("t0_ms"))))
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
