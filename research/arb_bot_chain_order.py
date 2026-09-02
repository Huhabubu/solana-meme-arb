#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为套利事件窗口补充 Solana 真实链上顺序。

输入：arb_bot_event_report.py 生成的 top_event_windows.csv / top_events_summary.csv。
输出：
- top_event_windows_chain_ordered.csv：每条 OKX 成交补充 slot + transactionIndex 后重排；
- event_chain_summary.csv：每个大额事件的大单/目标机器人真实链上相对顺序；
- event_blocks_chain_ordered.md：按大额买入/卖出分类、逐事件块展开的链上顺序记录。

排序规则：
1. 优先使用 (slot, transaction_index)；
2. 同一个 txHash 的 BUY/SELL 记录属于同一链上交易，无法仅凭 OKX 行确定内部 CPI 顺序，保持 OKX 行顺序；
3. 查询失败时回退到 OKX timestamp_ms，并显式标记 chain_order_resolved=false。

RPC：
- 优先 --rpc-url；
- 否则 HELIUS_RPC_URL；
- 否则用 HELIUS_API_KEY 拼接 Helius Mainnet RPC。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BOT_DEFAULT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"


def truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes"}


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


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def rpc_call(rpc_url: str, method: str, params: List[Any], *, retries: int = 5, timeout: int = 30) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                rpc_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "solana-meme-arb-research/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("error"):
                raise RuntimeError(f"RPC {method} error: {body['error']}")
            return body.get("result")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
    raise RuntimeError(f"RPC {method} failed after {retries} attempts: {last}")


def resolve_rpc_url(cli: str) -> str:
    if cli:
        return cli
    env_url = os.getenv("HELIUS_RPC_URL", "").strip()
    if env_url:
        return env_url
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit("RPC missing: pass --rpc-url or set HELIUS_RPC_URL / HELIUS_API_KEY")


def unique_hashes(rows: Iterable[Dict[str, str]]) -> List[str]:
    seen = set()
    out: List[str] = []
    for row in rows:
        h = str(row.get("trade_tx_hash") or "").strip()
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def fetch_tx_slots(rpc_url: str, hashes: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for i, h in enumerate(hashes, start=1):
        try:
            result = rpc_call(
                rpc_url,
                "getTransaction",
                [
                    h,
                    {
                        "commitment": "finalized",
                        "encoding": "json",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if result is None:
                out[h] = {"slot": None, "block_time": None, "error": "transaction_not_found"}
            else:
                out[h] = {
                    "slot": result.get("slot"),
                    "block_time": result.get("blockTime"),
                    "error": "",
                }
        except Exception as exc:  # noqa: BLE001 - research batch should continue
            out[h] = {"slot": None, "block_time": None, "error": str(exc)}
        if i % 25 == 0 or i == len(hashes):
            resolved = sum(1 for x in out.values() if x.get("slot") is not None)
            print(f"getTransaction {i}/{len(hashes)} resolved={resolved}")
        time.sleep(0.01)
    return out


def fetch_slot_indexes(
    rpc_url: str,
    tx_meta: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, int], Dict[int, str]]:
    by_slot: Dict[int, List[str]] = defaultdict(list)
    for h, meta in tx_meta.items():
        slot = meta.get("slot")
        if slot is not None:
            by_slot[int(slot)].append(h)

    indexes: Dict[str, int] = {}
    slot_errors: Dict[int, str] = {}
    slots = sorted(by_slot)
    for i, slot in enumerate(slots, start=1):
        try:
            block = rpc_call(
                rpc_url,
                "getBlock",
                [
                    slot,
                    {
                        "commitment": "finalized",
                        "transactionDetails": "signatures",
                        "rewards": False,
                    },
                ],
            )
            if block is None:
                slot_errors[slot] = "block_not_found"
                continue
            sigs = block.get("signatures") or []
            pos = {sig: idx for idx, sig in enumerate(sigs)}
            for h in by_slot[slot]:
                if h in pos:
                    indexes[h] = pos[h]
                else:
                    tx_meta[h]["error"] = (tx_meta[h].get("error") or "") + " signature_not_in_block"
        except Exception as exc:  # noqa: BLE001
            slot_errors[slot] = str(exc)
        if i % 20 == 0 or i == len(slots):
            print(f"getBlock {i}/{len(slots)} tx_indexes={len(indexes)}")
        time.sleep(0.01)
    return indexes, slot_errors


def chain_key(row: Dict[str, Any]) -> Tuple[int, int, int, str, int]:
    resolved = truthy(row.get("chain_order_resolved"))
    if resolved:
        return (
            0,
            inum(row.get("chain_slot")),
            inum(row.get("chain_tx_index")),
            str(row.get("trade_tx_hash") or ""),
            inum(row.get("source_row_index")),
        )
    return (
        1,
        inum(row.get("trade_timestamp_ms")),
        0,
        str(row.get("trade_tx_hash") or ""),
        inum(row.get("source_row_index")),
    )


def relation(trigger_row: Dict[str, Any], bot_row: Dict[str, Any]) -> str:
    if not truthy(trigger_row.get("chain_order_resolved")) or not truthy(bot_row.get("chain_order_resolved")):
        return "UNRESOLVED"
    a = (inum(trigger_row.get("chain_slot")), inum(trigger_row.get("chain_tx_index")))
    b = (inum(bot_row.get("chain_slot")), inum(bot_row.get("chain_tx_index")))
    if b > a:
        return "BOT_AFTER_TRIGGER"
    if b < a:
        return "BOT_BEFORE_TRIGGER"
    return "SAME_TX"


def classify(side: str) -> str:
    return "大额买入触发" if str(side).upper() == "BUY" else "大额卖出触发"


def main() -> None:
    p = argparse.ArgumentParser(description="用 Solana slot + transactionIndex 重排套利事件窗口")
    p.add_argument("--input-dir", default="research/output/arb_bot_events/top10")
    p.add_argument("--output-dir", default="research/output/arb_bot_events/top10/chain_order")
    p.add_argument("--rpc-url", default="")
    p.add_argument("--bot", default=BOT_DEFAULT)
    args = p.parse_args()

    src = Path(args.input_dir)
    details = read_csv(src / "top_event_windows.csv")
    summaries = read_csv(src / "top_events_summary.csv")
    if not details:
        raise SystemExit("top_event_windows.csv is empty")

    rpc_url = resolve_rpc_url(args.rpc_url)
    hashes = unique_hashes(details)
    print(f"unique_tx_hashes={len(hashes)}")

    tx_meta = fetch_tx_slots(rpc_url, hashes)
    tx_indexes, slot_errors = fetch_slot_indexes(rpc_url, tx_meta)

    enriched: List[Dict[str, Any]] = []
    for source_idx, row in enumerate(details):
        h = str(row.get("trade_tx_hash") or "")
        meta = tx_meta.get(h, {})
        slot = meta.get("slot")
        idx = tx_indexes.get(h)
        out: Dict[str, Any] = dict(row)
        out["source_row_index"] = source_idx
        out["chain_slot"] = "" if slot is None else int(slot)
        out["chain_tx_index"] = "" if idx is None else int(idx)
        out["chain_tx_position"] = "" if idx is None else int(idx) + 1
        out["chain_block_time"] = "" if meta.get("block_time") is None else int(meta["block_time"])
        out["chain_order_resolved"] = slot is not None and idx is not None
        out["chain_rpc_error"] = str(meta.get("error") or "").strip()
        enriched.append(out)

    by_event: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        by_event[str(row.get("event_id") or "")].append(row)
    for rows in by_event.values():
        rows.sort(key=chain_key)
        for seq, row in enumerate(rows, start=1):
            row["event_chain_sequence"] = seq

    ordered = sorted(
        enriched,
        key=lambda r: (inum(r.get("rank")), inum(r.get("event_chain_sequence"))),
    )

    summary_by_event = {str(r.get("event_id") or ""): r for r in summaries}
    chain_summaries: List[Dict[str, Any]] = []
    for event_id, rows in sorted(by_event.items(), key=lambda kv: inum(summary_by_event.get(kv[0], {}).get("rank"))):
        base = summary_by_event.get(event_id, {})
        trigger_rows = [r for r in rows if truthy(r.get("is_trigger"))]
        bot_rows = [
            r
            for r in rows
            if truthy(r.get("is_bot_trade"))
            or str(r.get("trader_address") or "").lower() == args.bot.lower()
        ]
        trigger = trigger_rows[0] if trigger_rows else {}
        bot_txs: Dict[str, Dict[str, Any]] = {}
        for r in bot_rows:
            h = str(r.get("trade_tx_hash") or "")
            if h and h not in bot_txs:
                bot_txs[h] = r
        relations = [relation(trigger, r) for r in bot_txs.values()]
        if relations and all(x == "BOT_AFTER_TRIGGER" for x in relations):
            event_relation = "ALL_BOTS_AFTER_TRIGGER"
        elif relations and all(x == "BOT_BEFORE_TRIGGER" for x in relations):
            event_relation = "ALL_BOTS_BEFORE_TRIGGER"
        elif relations and all(x != "UNRESOLVED" for x in relations):
            event_relation = "MIXED_CHAIN_ORDER"
        else:
            event_relation = "UNRESOLVED"

        chain_summaries.append(
            {
                "rank": base.get("rank", ""),
                "category": classify(str(base.get("trigger_side") or "")),
                "symbol": base.get("symbol", ""),
                "mint": base.get("mint", ""),
                "trigger_usd": base.get("trigger_usd", ""),
                "trigger_side": base.get("trigger_side", ""),
                "trigger_tx_hash": base.get("trigger_tx_hash", ""),
                "trigger_slot": trigger.get("chain_slot", ""),
                "trigger_tx_index": trigger.get("chain_tx_index", ""),
                "trigger_chain_sequence": trigger.get("event_chain_sequence", ""),
                "bot_tx_count": len(bot_txs),
                "bot_tx_hashes": " | ".join(bot_txs.keys()),
                "bot_slot_indexes": " | ".join(
                    f"{r.get('chain_slot','')}:{r.get('chain_tx_index','')}" for r in bot_txs.values()
                ),
                "bot_chain_sequences": " | ".join(str(r.get("event_chain_sequence", "")) for r in bot_txs.values()),
                "chain_relation": event_relation,
                "resolved_trade_rows": sum(1 for r in rows if truthy(r.get("chain_order_resolved"))),
                "window_trade_rows": len(rows),
                "event_id": event_id,
            }
        )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "top_event_windows_chain_ordered.csv", ordered)
    write_csv(out_dir / "event_chain_summary.csv", chain_summaries)

    lines: List[str] = []
    for category in ("大额买入触发", "大额卖出触发"):
        lines.append(f"# {category}")
        lines.append("")
        cat_events = [r for r in chain_summaries if r["category"] == category]
        for ev in cat_events:
            lines.append(
                f"## 事件 #{inum(ev['rank']):02d} {ev['symbol']} | 中心大单 ${fnum(ev['trigger_usd']):,.2f} | {ev['chain_relation']}"
            )
            lines.append(f"- Mint: `{ev['mint']}`")
            lines.append(
                f"- 中心大单: `{ev['trigger_tx_hash']}` | slot/index=`{ev['trigger_slot']}:{ev['trigger_tx_index']}`"
            )
            lines.append(f"- 机器人 Tx: `{ev['bot_tx_hashes']}`")
            lines.append("")
            lines.append("| 链上序号 | slot:index | 标注 | OKX时间ms | 方向 | 金额USD | TxHash |")
            lines.append("|---:|---|---|---:|---|---:|---|")
            event_rows = by_event[str(ev["event_id"])]
            for r in event_rows:
                if truthy(r.get("is_trigger")):
                    mark = "**中心大单**"
                elif truthy(r.get("is_bot_trade")) or str(r.get("trader_address") or "").lower() == args.bot.lower():
                    mark = "**套利机器人 MRiYA4**"
                elif truthy(r.get("is_large_trade")):
                    mark = "其他大额交易"
                else:
                    mark = "普通交易"
                slot_index = (
                    f"{r.get('chain_slot')}:{r.get('chain_tx_index')}"
                    if truthy(r.get("chain_order_resolved"))
                    else "未解析"
                )
                lines.append(
                    f"| {r.get('event_chain_sequence')} | {slot_index} | {mark} | {r.get('trade_timestamp_ms')} | "
                    f"{r.get('trade_side')} | {fnum(r.get('trade_usd')):.6f} | `{r.get('trade_tx_hash')}` |"
                )
            lines.append("")
    (out_dir / "event_blocks_chain_ordered.md").write_text("\n".join(lines), encoding="utf-8")

    resolved = sum(1 for r in ordered if truthy(r.get("chain_order_resolved")))
    after = sum(1 for r in chain_summaries if r["chain_relation"] == "ALL_BOTS_AFTER_TRIGGER")
    before = sum(1 for r in chain_summaries if r["chain_relation"] == "ALL_BOTS_BEFORE_TRIGGER")
    mixed = sum(1 for r in chain_summaries if r["chain_relation"] == "MIXED_CHAIN_ORDER")
    unresolved = sum(1 for r in chain_summaries if r["chain_relation"] == "UNRESOLVED")
    meta = {
        "unique_tx_hashes": len(hashes),
        "resolved_trade_rows": resolved,
        "total_trade_rows": len(ordered),
        "unique_slots": len({m.get('slot') for m in tx_meta.values() if m.get('slot') is not None}),
        "slot_errors": slot_errors,
        "event_relations": {
            "all_bots_after_trigger": after,
            "all_bots_before_trigger": before,
            "mixed_chain_order": mixed,
            "unresolved": unresolved,
        },
    }
    (out_dir / "chain_order_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    for r in chain_summaries:
        print(
            f"#{r['rank']} {r['symbol']} trigger={r['trigger_slot']}:{r['trigger_tx_index']} "
            f"bots={r['bot_slot_indexes']} relation={r['chain_relation']} "
            f"resolved={r['resolved_trade_rows']}/{r['window_trade_rows']}"
        )


if __name__ == "__main__":
    main()
