#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOL 首笔建仓事件研究（只读）。

目标：验证“看到公开 KOL 对一个 Mint 的首笔明显买入后，延迟 1/2/3/5/10 秒跟入，
是否仍有可复制收益”，并统计 KOL 买入后的跟单压力。

数据口径：
1. pnl/token-list 只用于发现最近活跃 Mint；不使用其交易次数作为真实成交次数。
2. trading-history/filter-list + userAddressList=[KOL] 用于恢复 KOL×Mint 的真实历史成交。
3. trading-history/filter-list + userAddressList=[] 用于事件窗口全市场逐笔成交。

注意：OKX 网页内部接口可能变化；结果属于研究数据，不是实盘执行报价。
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
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DELAYS_S = (1, 2, 3, 5, 10)
HORIZONS_S = (5, 10, 30, 60, 300)
PRESSURE_WINDOWS_S = (1, 3, 5, 10)


def fnum(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def inum(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    referer: str,
    retries: int = 3,
    timeout: int = 15,
) -> Tuple[int, Dict[str, Any]]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": referer,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body)
        except Exception as exc:  # noqa: BLE001 - standalone research script
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries} attempts: {last_error}")


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
    url = PNL_URL + "?" + urllib.parse.urlencode(params)
    status, body = request_json(
        url,
        referer="https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
    )
    if status != 200 or str(body.get("code")) != "0":
        raise RuntimeError(f"pnl failed status={status} code={body.get('code')} msg={body.get('msg')}")
    return ((body.get("data") or {}).get("tokenList") or [])


def trade_payload(
    chain_id: str,
    mint: str,
    users: List[str],
    data_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
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
        payload["dataId"] = data_id
    return payload


def fetch_trade_page(
    chain_id: str,
    mint: str,
    users: List[str],
    data_id: Optional[str],
) -> Dict[str, Any]:
    url = TRADE_URL + "?t=" + str(int(time.time() * 1000))
    status, body = request_json(
        url,
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
            r.get("timestamp"),
            r.get("userAddress"),
            r.get("isBuy"),
            r.get("volume"),
            r.get("price"),
        )
        out[key] = r
    return list(out.values())


def fetch_kol_history(
    wallet: str,
    chain_id: str,
    mint: str,
    max_rows: int,
) -> Tuple[List[Dict[str, Any]], bool]:
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
        has_more = str(data.get("hasMore", "0")) == "1"
        next_id = page[-1].get("id")
        if not has_more:
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
    """从最新成交向过去分页，直到覆盖 start_ms。"""
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

        has_more = str(data.get("hasMore", "0")) == "1"
        next_id = page[-1].get("id")
        if not has_more or not next_id or next_id == data_id:
            break
        data_id = next_id
        time.sleep(0.02)

    rows = dedup_rows(rows)
    rows.sort(key=lambda x: inum(x.get("timestamp")))
    return rows, covered_start, pages


def is_buy(row: Dict[str, Any]) -> bool:
    return str(row.get("isBuy")) == "1"


def row_price(row: Optional[Dict[str, Any]]) -> Optional[float]:
    if not row:
        return None
    p = fnum(row.get("price"))
    return p if p > 0 else None


def first_trade_at_or_after(
    rows: List[Dict[str, Any]],
    target_ms: int,
    max_wait_ms: int = 5000,
) -> Optional[Dict[str, Any]]:
    end = target_ms + max_wait_ms
    for r in rows:
        ts = inum(r.get("timestamp"))
        if target_ms <= ts <= end and row_price(r):
            return r
        if ts > end:
            break
    return None


def bps_return(entry: Optional[float], exit_: Optional[float]) -> Optional[float]:
    if not entry or not exit_ or entry <= 0:
        return None
    return (exit_ / entry - 1.0) * 10000.0


def pressure_metrics(
    rows: List[Dict[str, Any]],
    kol_address: str,
    t0_ms: int,
    seconds: int,
) -> Dict[str, Any]:
    end = t0_ms + seconds * 1000
    market = [
        r
        for r in rows
        if t0_ms < inum(r.get("timestamp")) <= end
        and str(r.get("userAddress") or "").lower() != kol_address.lower()
    ]
    buys = [r for r in market if is_buy(r)]
    sells = [r for r in market if not is_buy(r)]
    buy_usd = sum(fnum(r.get("volume")) for r in buys)
    sell_usd = sum(fnum(r.get("volume")) for r in sells)
    unique_buyers = len({str(r.get("userAddress") or "").lower() for r in buys if r.get("userAddress")})
    return {
        f"followers_{seconds}s_buy_trades": len(buys),
        f"followers_{seconds}s_unique_buyers": unique_buyers,
        f"followers_{seconds}s_buy_usd": round(buy_usd, 6),
        f"followers_{seconds}s_sell_usd": round(sell_usd, 6),
        f"followers_{seconds}s_net_buy_usd": round(buy_usd - sell_usd, 6),
    }


def baseline_buy_usd(
    rows: List[Dict[str, Any]],
    kol_address: str,
    start_ms: int,
    end_ms: int,
) -> float:
    return sum(
        fnum(r.get("volume"))
        for r in rows
        if start_ms <= inum(r.get("timestamp")) < end_ms
        and is_buy(r)
        and str(r.get("userAddress") or "").lower() != kol_address.lower()
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

    first_buy = buys[0]
    first_buy_usd = fnum(first_buy.get("volume"))
    t0 = inum(first_buy.get("timestamp"))
    if first_buy_usd < args.min_first_buy_usd:
        return None

    now_ms = int(time.time() * 1000)
    age_hours = (now_ms - t0) / 3_600_000 if t0 else math.inf
    if age_hours < 0 or age_hours > args.max_event_age_hours:
        return None

    later = [r for r in chron if inum(r.get("timestamp")) > t0]
    later_sells = [r for r in later if not is_buy(r)]
    first_sell = later_sells[0] if later_sells else None
    first_sell_ts = inum(first_sell.get("timestamp")) if first_sell else None
    first_sell_delay_s = ((first_sell_ts - t0) / 1000.0) if first_sell_ts else None

    pre_sell_buys = [
        r for r in later if is_buy(r) and (first_sell_ts is None or inum(r.get("timestamp")) < first_sell_ts)
    ]
    small_probe_buys = [r for r in pre_sell_buys if fnum(r.get("volume")) <= first_buy_usd * 0.10]

    start_ms = t0 - args.pre_window_seconds * 1000
    end_ms = t0 + args.post_window_seconds * 1000
    market, market_complete, market_pages = fetch_market_window(
        chain_id,
        mint,
        start_ms,
        end_ms,
        args.max_market_pages,
    )

    event: Dict[str, Any] = {
        "kol": kol["name"],
        "kol_address": wallet,
        "chain": kol["chain"],
        "chain_id": chain_id,
        "tier": kol["tier"],
        "symbol": pnl_item.get("tokenSymbol"),
        "mint": mint,
        "t0_ms": t0,
        "event_age_hours": round(age_hours, 4),
        "first_buy_usd": round(first_buy_usd, 6),
        "first_buy_price": row_price(first_buy),
        "first_buy_id": first_buy.get("id"),
        "history_rows": len(chron),
        "history_complete": history_complete,
        "kol_buy_rows": len(buys),
        "kol_sell_rows": len([r for r in chron if not is_buy(r)]),
        "first_sell_delay_s": round(first_sell_delay_s, 3) if first_sell_delay_s is not None else None,
        "first_sell_usd": round(fnum(first_sell.get("volume")), 6) if first_sell else None,
        "pre_first_sell_extra_buys": len(pre_sell_buys),
        "pre_first_sell_small_probe_buys": len(small_probe_buys),
        "small_probe_buy_usd": [round(fnum(r.get("volume")), 6) for r in small_probe_buys[:20]],
        "market_rows": len(market),
        "market_window_complete": market_complete,
        "market_pages": market_pages,
    }

    pre5 = baseline_buy_usd(market, wallet, t0 - 5000, t0)
    post5 = baseline_buy_usd(market, wallet, t0, t0 + 5000)
    event["followers_pre5s_buy_usd"] = round(pre5, 6)
    event["followers_post5s_buy_usd"] = round(post5, 6)
    event["followers_post5_vs_pre5_ratio"] = round(post5 / pre5, 6) if pre5 > 0 else None

    for window in PRESSURE_WINDOWS_S:
        event.update(pressure_metrics(market, wallet, t0, window))

    first_sell_market_price = None
    if first_sell_ts:
        first_sell_market_price = row_price(first_trade_at_or_after(market, first_sell_ts, 5000))
    event["market_price_at_kol_first_sell"] = first_sell_market_price

    for delay in DELAYS_S:
        entry_row = first_trade_at_or_after(market, t0 + delay * 1000, 5000)
        entry_price = row_price(entry_row)
        entry_ts = inum(entry_row.get("timestamp")) if entry_row else None
        event[f"entry_{delay}s_price"] = entry_price
        event[f"entry_{delay}s_actual_delay_ms"] = (entry_ts - t0) if entry_ts else None

        if entry_price and entry_ts:
            future_prices = [
                row_price(r)
                for r in market
                if entry_ts <= inum(r.get("timestamp")) <= t0 + args.post_window_seconds * 1000
                and row_price(r)
            ]
            if future_prices:
                max_p = max(future_prices)
                min_p = min(future_prices)
                event[f"entry_{delay}s_mfe_bps"] = round(bps_return(entry_price, max_p) or 0, 3)
                event[f"entry_{delay}s_mae_bps"] = round(bps_return(entry_price, min_p) or 0, 3)
            else:
                event[f"entry_{delay}s_mfe_bps"] = None
                event[f"entry_{delay}s_mae_bps"] = None
        else:
            event[f"entry_{delay}s_mfe_bps"] = None
            event[f"entry_{delay}s_mae_bps"] = None

        to_sell = bps_return(entry_price, first_sell_market_price)
        event[f"entry_{delay}s_to_kol_first_sell_bps"] = round(to_sell, 3) if to_sell is not None else None

        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            mark_row = first_trade_at_or_after(market, t0 + horizon * 1000, 5000)
            mark_price = row_price(mark_row)
            ret = bps_return(entry_price, mark_price)
            event[f"entry_{delay}s_to_{horizon}s_bps"] = round(ret, 3) if ret is not None else None

    return event


def median_or_none(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 3) if values else None


def mean_or_none(values: List[float]) -> Optional[float]:
    return round(statistics.fmean(values), 3) if values else None


def summarize(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "event_count": len(events),
        "by_kol": {},
        "delay_metrics": {},
    }
    by_kol: Dict[str, int] = defaultdict(int)
    for e in events:
        by_kol[e["kol"]] += 1
    summary["by_kol"] = dict(sorted(by_kol.items()))

    for delay in DELAYS_S:
        metrics: Dict[str, Any] = {}
        sell_key = f"entry_{delay}s_to_kol_first_sell_bps"
        sell_vals = [float(e[sell_key]) for e in events if e.get(sell_key) is not None]
        metrics["to_kol_first_sell"] = {
            "n": len(sell_vals),
            "median_bps": median_or_none(sell_vals),
            "mean_bps": mean_or_none(sell_vals),
            "win_rate": round(sum(v > 0 for v in sell_vals) / len(sell_vals), 4) if sell_vals else None,
        }
        for horizon in HORIZONS_S:
            if horizon <= delay:
                continue
            key = f"entry_{delay}s_to_{horizon}s_bps"
            vals = [float(e[key]) for e in events if e.get(key) is not None]
            metrics[f"to_{horizon}s"] = {
                "n": len(vals),
                "median_bps": median_or_none(vals),
                "mean_bps": mean_or_none(vals),
                "win_rate": round(sum(v > 0 for v in vals) / len(vals), 4) if vals else None,
            }
        mfe_key = f"entry_{delay}s_mfe_bps"
        mae_key = f"entry_{delay}s_mae_bps"
        mfe = [float(e[mfe_key]) for e in events if e.get(mfe_key) is not None]
        mae = [float(e[mae_key]) for e in events if e.get(mae_key) is not None]
        metrics["median_mfe_bps"] = median_or_none(mfe)
        metrics["median_mae_bps"] = median_or_none(mae)
        summary["delay_metrics"][str(delay)] = metrics
    return summary


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
    args = parse_args()
    wanted_chains = {x.strip() for x in args.chains.split(",") if x.strip()}
    wanted_tiers = {x.strip() for x in args.tiers.split(",") if x.strip()}

    watch = json.loads(Path(args.watchlist).read_text(encoding="utf-8"))
    kols = [
        k
        for k in watch["kols"]
        if k["chain_id"] in wanted_chains and k["tier"] in wanted_tiers
    ]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    print(f"KOL_COUNT {len(kols)}")
    for kol in kols:
        print(f"\n=== {kol['name']} {kol['address']} chain={kol['chain_id']} ===")
        try:
            mints = fetch_recent_mints(kol["address"], kol["chain_id"], args.recent_mints)
        except Exception as exc:  # noqa: BLE001
            errors.append({"kol": kol["name"], "stage": "recent_mints", "error": str(exc)})
            print("ERROR recent_mints", exc)
            continue

        for item in mints:
            mint = item.get("tokenContractAddress")
            if not mint:
                continue
            try:
                history, complete = fetch_kol_history(
                    kol["address"], kol["chain_id"], mint, args.max_kol_history_rows
                )
                if not complete:
                    print("SKIP incomplete_history", kol["name"], item.get("tokenSymbol"), mint, len(history))
                    continue
                event = analyze_event(kol, item, history, complete, args)
                if event:
                    events.append(event)
                    print("EVENT", json.dumps({
                        "kol": event["kol"],
                        "symbol": event["symbol"],
                        "mint": event["mint"],
                        "first_buy_usd": event["first_buy_usd"],
                        "first_sell_delay_s": event["first_sell_delay_s"],
                        "probe_buys": event["pre_first_sell_small_probe_buys"],
                        "market_rows": event["market_rows"],
                        "market_complete": event["market_window_complete"],
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
    print("OUTPUT_DIR", str(out_dir))


if __name__ == "__main__":
    main()
