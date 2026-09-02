#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Solana 套利机器人“大额交易事件窗口”研究。

目标：
1. 从 OKX Web3 PnL 接口获取指定套利机器人近期活跃 Mint；
2. 逐个 Mint 获取该地址的历史成交；
3. 以每个套利交易 txHash 为锚点，抓取该 Mint 前后若干秒的全市场逐笔成交；
4. 在窗口内寻找非机器人最大成交作为 trigger_candidate；
5. 以 trigger_candidate 为中心输出前后一段时间的完整交易记录，保留时间、金额、方向、地址、txHash；
6. 明确标记套利机器人交易、大额候选交易以及二者相对时间。

注意：
- 本脚本是只读历史研究，不提交链上交易。
- “大额”阈值通过 --min-large-usd 配置；即使候选未达到阈值，也会输出最大非机器人成交，避免阈值过高导致事件丢失。
- OKX 接口属于 Web3 市场数据接口；时间字段按接口返回的 epoch 毫秒处理。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BOT_ADDRESS_DEFAULT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"
CHAIN_ID_DEFAULT = "501"  # OKX Web3: Solana

PNL_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/pnl/token-list"
TRADE_URL = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TRADE_PAGE_LIMIT = 100


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
) -> Dict[str, Any]:
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
                body = json.loads(resp.read().decode("utf-8"))
            if str(body.get("code")) != "0":
                raise RuntimeError(f"OKX code={body.get('code')} msg={body.get('msg')}")
            return body
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
        "filterRisk": "false",
        "filterSmallBalance": "false",
        "t": str(int(time.time() * 1000)),
    }
    body = request_json(
        PNL_URL + "?" + urllib.parse.urlencode(params),
        referer="https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
    )
    return ((body.get("data") or {}).get("tokenList") or [])


def trade_payload(
    chain_id: str,
    mint: str,
    users: List[str],
    data_id: Optional[str],
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {
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
    }
    if start_ms is not None:
        filters["startTime"] = int(start_ms)
    if end_ms is not None:
        filters["endTime"] = int(end_ms)

    payload: Dict[str, Any] = {
        "desc": True,
        "orderBy": "timestamp",
        "limit": TRADE_PAGE_LIMIT,
        "tradingHistoryFilter": filters,
    }
    if data_id is not None:
        payload["dataId"] = str(data_id)
    return payload


def fetch_trade_page(
    chain_id: str,
    mint: str,
    users: List[str],
    data_id: Optional[str],
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Dict[str, Any]:
    body = request_json(
        TRADE_URL + "?t=" + str(int(time.time() * 1000)),
        method="POST",
        payload=trade_payload(chain_id, mint, users, data_id, start_ms=start_ms, end_ms=end_ms),
        referer="https://web3.okx.com/zh-hans/market/dex",
    )
    return body.get("data") or {}


def dedup_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        key = row.get("id") or (
            row.get("timestamp"),
            row.get("userAddress"),
            row.get("isBuy"),
            row.get("volume"),
            row.get("price"),
            tx_hash(row),
        )
        out[key] = row
    return list(out.values())


def fetch_wallet_mint_history(
    wallet: str,
    chain_id: str,
    mint: str,
    max_rows: int,
) -> Tuple[List[Dict[str, Any]], bool, int]:
    rows: List[Dict[str, Any]] = []
    data_id: Optional[str] = None
    pages = 0
    complete = False
    max_pages = max(1, math.ceil(max_rows / TRADE_PAGE_LIMIT) + 1)

    while len(rows) < max_rows and pages < max_pages:
        data = fetch_trade_page(chain_id, mint, [wallet], data_id)
        page = data.get("list") or []
        pages += 1
        if not page:
            complete = True
            break
        rows.extend(page)
        if str(data.get("hasMore", "0")) != "1":
            complete = True
            break
        next_id = page[-1].get("id")
        if not next_id or str(next_id) == str(data_id):
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
    rows: List[Dict[str, Any]] = []
    data_id: Optional[str] = str(end_ms)
    pages = 0
    complete = False

    while pages < max_pages:
        data = fetch_trade_page(
            chain_id,
            mint,
            [],
            data_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        page = data.get("list") or []
        pages += 1
        if not page:
            complete = True
            break

        for row in page:
            ts = inum(row.get("timestamp"))
            if start_ms <= ts <= end_ms:
                rows.append(row)

        oldest = min(inum(r.get("timestamp")) for r in page)
        if oldest <= start_ms or str(data.get("hasMore", "0")) != "1":
            complete = True
            break
        next_id = page[-1].get("id")
        if not next_id or str(next_id) == str(data_id):
            break
        data_id = str(next_id)
        time.sleep(0.005)

    rows = dedup_rows(rows)
    rows.sort(key=lambda r: (inum(r.get("timestamp")), str(r.get("id") or "")))
    return rows, complete, pages


def group_bot_transactions(rows: List[Dict[str, Any]], wallet: str) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        h = tx_hash(row)
        if h:
            grouped[h].append(row)

    out: List[Dict[str, Any]] = []
    for h, xs in grouped.items():
        xs.sort(key=lambda r: inum(r.get("timestamp")))
        ts_values = [inum(r.get("timestamp")) for r in xs if inum(r.get("timestamp")) > 0]
        if not ts_values:
            continue
        buys = [r for r in xs if str(r.get("isBuy")) == "1"]
        sells = [r for r in xs if str(r.get("isBuy")) != "1"]
        out.append(
            {
                "tx_hash": h,
                "timestamp_ms": min(ts_values),
                "rows": xs,
                "buy_rows": len(buys),
                "sell_rows": len(sells),
                "buy_usd": sum(fnum(r.get("volume")) for r in buys),
                "sell_usd": sum(fnum(r.get("volume")) for r in sells),
                "looks_like_two_sided_arb": bool(buys and sells),
                "wallet": wallet,
            }
        )
    out.sort(key=lambda x: int(x["timestamp_ms"]), reverse=True)
    return out


def is_bot_row(row: Dict[str, Any], bot_address: str, bot_tx_hash: str) -> bool:
    user = str(row.get("userAddress") or "")
    return user.lower() == bot_address.lower() or tx_hash(row) == bot_tx_hash


def select_trigger_candidate(
    rows: List[Dict[str, Any]],
    bot_address: str,
    bot_tx_hash: str,
) -> Optional[Dict[str, Any]]:
    candidates = [r for r in rows if not is_bot_row(r, bot_address, bot_tx_hash)]
    if not candidates:
        return None
    return max(candidates, key=lambda r: fnum(r.get("volume")))


def iso_ms(ts_ms: int) -> str:
    if ts_ms <= 0:
        return ""
    sec = ts_ms // 1000
    ms = ts_ms % 1000
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec)) + f".{ms:03d}"


def event_rows(
    *,
    event_id: str,
    mint: str,
    symbol: str,
    pnl_item: Dict[str, Any],
    bot_tx: Dict[str, Any],
    trigger: Dict[str, Any],
    rows: List[Dict[str, Any]],
    bot_address: str,
    min_large_usd: float,
    market_complete: bool,
) -> List[Dict[str, Any]]:
    trigger_ts = inum(trigger.get("timestamp"))
    trigger_hash = tx_hash(trigger) or ""
    bot_ts = int(bot_tx["timestamp_ms"])
    result: List[Dict[str, Any]] = []
    for row in rows:
        ts = inum(row.get("timestamp"))
        h = tx_hash(row) or ""
        user = str(row.get("userAddress") or "")
        usd = fnum(row.get("volume"))
        is_bot = user.lower() == bot_address.lower() or h == bot_tx["tx_hash"]
        is_trigger = h == trigger_hash and h != "" if trigger_hash else row is trigger
        result.append(
            {
                "event_id": event_id,
                "symbol": symbol,
                "mint": mint,
                "pnl_total_usd": fnum(pnl_item.get("totalPnl")),
                "pnl_total_pct": fnum(pnl_item.get("totalPnlPercentage")),
                "trigger_time": iso_ms(trigger_ts),
                "trigger_timestamp_ms": trigger_ts,
                "trigger_tx_hash": trigger_hash,
                "trigger_usd": round(fnum(trigger.get("volume")), 6),
                "trigger_side": "BUY" if str(trigger.get("isBuy")) == "1" else "SELL",
                "trigger_large": fnum(trigger.get("volume")) >= min_large_usd,
                "bot_time": iso_ms(bot_ts),
                "bot_timestamp_ms": bot_ts,
                "bot_tx_hash": bot_tx["tx_hash"],
                "bot_buy_usd": round(float(bot_tx["buy_usd"]), 6),
                "bot_sell_usd": round(float(bot_tx["sell_usd"]), 6),
                "bot_two_sided": bool(bot_tx["looks_like_two_sided_arb"]),
                "bot_minus_trigger_ms": bot_ts - trigger_ts,
                "market_window_complete": market_complete,
                "trade_time": iso_ms(ts),
                "trade_timestamp_ms": ts,
                "relative_to_trigger_ms": ts - trigger_ts,
                "trade_side": "BUY" if str(row.get("isBuy")) == "1" else "SELL",
                "trade_usd": round(usd, 6),
                "trade_price": fnum(row.get("price")),
                "trader_address": user,
                "trade_tx_hash": h,
                "is_trigger": is_trigger,
                "is_bot_trade": is_bot,
                "is_large_trade": usd >= min_large_usd,
            }
        )
    result.sort(key=lambda r: (int(r["trade_timestamp_ms"]), str(r["trade_tx_hash"])))
    return result


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description="套利机器人 → Mint → 大额交易中心事件窗口研究")
    p.add_argument("--bot", default=BOT_ADDRESS_DEFAULT)
    p.add_argument("--chain-id", default=CHAIN_ID_DEFAULT)
    p.add_argument("--mint-limit", type=int, default=30, help="从 PnL 接口读取的近期 Mint 数量")
    p.add_argument("--bot-history-rows", type=int, default=300, help="每个 Mint 最多读取多少条机器人历史成交")
    p.add_argument("--arb-events-per-mint", type=int, default=10, help="每个 Mint 最多研究多少笔机器人 txHash")
    p.add_argument("--search-window-ms", type=int, default=2000, help="先围绕机器人交易搜索触发候选的前后窗口")
    p.add_argument("--event-window-ms", type=int, default=2000, help="最终围绕触发候选输出的前后窗口")
    p.add_argument("--min-large-usd", type=float, default=1000.0, help="标记大额成交的 USD 阈值；候选未达阈值仍保留")
    p.add_argument("--max-market-pages", type=int, default=50)
    p.add_argument("--only-two-sided", action="store_true", help="只研究同 txHash 同时有 BUY 与 SELL 的机器人交易")
    p.add_argument("--output-dir", default="research/output/arb_bot_events")
    args = p.parse_args()

    if args.search_window_ms <= 0 or args.event_window_ms <= 0:
        raise SystemExit("window ms must be > 0")
    if args.min_large_usd < 0:
        raise SystemExit("--min-large-usd must be >= 0")

    print(f"BOT={args.bot}")
    print(f"CHAIN_ID={args.chain_id}")
    print("获取近期 PnL/Mint 列表...")
    pnl_items = fetch_recent_mints(args.bot, args.chain_id, args.mint_limit)
    print(f"近期 Mint: {len(pnl_items)}")

    all_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    seen_events: set[Tuple[str, str, str]] = set()

    for mint_index, pnl_item in enumerate(pnl_items, start=1):
        mint = str(pnl_item.get("tokenContractAddress") or "")
        symbol = str(pnl_item.get("tokenSymbol") or "")
        if not mint:
            continue
        print(f"[{mint_index}/{len(pnl_items)}] {symbol} {mint}")
        try:
            bot_history, bot_history_complete, bot_pages = fetch_wallet_mint_history(
                args.bot, args.chain_id, mint, args.bot_history_rows
            )
        except Exception as exc:  # noqa: BLE001 - continue research batch
            print(f"  bot history failed: {exc}")
            continue

        bot_txs = group_bot_transactions(bot_history, args.bot)
        if args.only_two_sided:
            bot_txs = [x for x in bot_txs if x["looks_like_two_sided_arb"]]
        bot_txs = bot_txs[: args.arb_events_per_mint]
        print(f"  bot rows={len(bot_history)} txs={len(bot_txs)} complete={bot_history_complete} pages={bot_pages}")

        for tx_index, bot_tx in enumerate(bot_txs, start=1):
            bot_ts = int(bot_tx["timestamp_ms"])
            search_start = bot_ts - args.search_window_ms
            search_end = bot_ts + args.search_window_ms
            try:
                search_rows, search_complete, search_pages = fetch_market_window(
                    args.chain_id, mint, search_start, search_end, args.max_market_pages
                )
            except Exception as exc:  # noqa: BLE001
                print(f"    tx {tx_index}: search window failed: {exc}")
                continue

            trigger = select_trigger_candidate(search_rows, args.bot, bot_tx["tx_hash"])
            if trigger is None:
                print(f"    tx {tx_index}: no non-bot trigger candidate")
                continue

            trigger_ts = inum(trigger.get("timestamp"))
            trigger_hash = tx_hash(trigger) or ""
            dedup_key = (mint, bot_tx["tx_hash"], trigger_hash or str(trigger_ts))
            if dedup_key in seen_events:
                continue
            seen_events.add(dedup_key)

            event_start = trigger_ts - args.event_window_ms
            event_end = trigger_ts + args.event_window_ms
            try:
                rows, complete, pages = fetch_market_window(
                    args.chain_id, mint, event_start, event_end, args.max_market_pages
                )
            except Exception as exc:  # noqa: BLE001
                print(f"    tx {tx_index}: event window failed: {exc}")
                continue

            event_id = f"{symbol or 'TOKEN'}-{bot_ts}-{bot_tx['tx_hash'][:8]}"
            normalized = event_rows(
                event_id=event_id,
                mint=mint,
                symbol=symbol,
                pnl_item=pnl_item,
                bot_tx=bot_tx,
                trigger=trigger,
                rows=rows,
                bot_address=args.bot,
                min_large_usd=args.min_large_usd,
                market_complete=complete,
            )
            all_rows.extend(normalized)
            summary_rows.append(
                {
                    "event_id": event_id,
                    "symbol": symbol,
                    "mint": mint,
                    "trigger_time": iso_ms(trigger_ts),
                    "trigger_timestamp_ms": trigger_ts,
                    "trigger_tx_hash": trigger_hash,
                    "trigger_usd": round(fnum(trigger.get("volume")), 6),
                    "trigger_side": "BUY" if str(trigger.get("isBuy")) == "1" else "SELL",
                    "trigger_large": fnum(trigger.get("volume")) >= args.min_large_usd,
                    "bot_time": iso_ms(bot_ts),
                    "bot_timestamp_ms": bot_ts,
                    "bot_tx_hash": bot_tx["tx_hash"],
                    "bot_buy_usd": round(float(bot_tx["buy_usd"]), 6),
                    "bot_sell_usd": round(float(bot_tx["sell_usd"]), 6),
                    "bot_two_sided": bool(bot_tx["looks_like_two_sided_arb"]),
                    "bot_minus_trigger_ms": bot_ts - trigger_ts,
                    "search_window_complete": search_complete,
                    "search_pages": search_pages,
                    "event_window_complete": complete,
                    "event_pages": pages,
                    "event_trade_rows": len(rows),
                    "pnl_total_usd": fnum(pnl_item.get("totalPnl")),
                    "pnl_total_pct": fnum(pnl_item.get("totalPnlPercentage")),
                }
            )
            print(
                f"    EVENT {event_id}: trigger=${fnum(trigger.get('volume')):.2f} "
                f"bot-trigger={bot_ts - trigger_ts}ms rows={len(rows)} complete={complete}"
            )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "event_windows.csv", all_rows)
    write_jsonl(out_dir / "event_windows.jsonl", all_rows)
    write_csv(out_dir / "events_summary.csv", summary_rows)
    (out_dir / "run_meta.json").write_text(
        json.dumps(
            {
                "bot": args.bot,
                "chain_id": args.chain_id,
                "mint_limit": args.mint_limit,
                "bot_history_rows": args.bot_history_rows,
                "arb_events_per_mint": args.arb_events_per_mint,
                "search_window_ms": args.search_window_ms,
                "event_window_ms": args.event_window_ms,
                "min_large_usd": args.min_large_usd,
                "only_two_sided": args.only_two_sided,
                "pnl_mints": len(pnl_items),
                "events": len(summary_rows),
                "event_trade_rows": len(all_rows),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n完成")
    print(f"events={len(summary_rows)} event_trade_rows={len(all_rows)}")
    print(f"summary={out_dir / 'events_summary.csv'}")
    print(f"windows={out_dir / 'event_windows.csv'}")


if __name__ == "__main__":
    main()
