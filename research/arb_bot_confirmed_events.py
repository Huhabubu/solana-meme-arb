#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从链上排序后的候选中生成“确认触发”的正式套利事件集。

正式事件条件：
- 中心大额交易与目标机器人交易均已解析 slot + transactionIndex；
- 目标机器人所有已识别套利 Tx 均在中心大额交易之后；
- 默认中心大额 >= 1000 USD；
- 通过后再按中心大额金额排序，取 Top N。

输出以“大额交易”为中心，保留完整前后窗口并按真实链上顺序排列。
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

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


def category(side: str) -> str:
    return "大额买入触发" if str(side).upper() == "BUY" else "大额卖出触发"


def row_chain_key(row: Dict[str, Any]) -> Tuple[int, int, int]:
    return (
        inum(row.get("chain_slot")),
        inum(row.get("chain_tx_index")),
        inum(row.get("source_row_index")),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="筛选链上确认的大额交易→套利机器人事件")
    p.add_argument("--input-dir", default="research/output/arb_bot_events/candidates/chain_order")
    p.add_argument("--output-dir", default="research/output/arb_bot_events/confirmed_top10")
    p.add_argument("--top-events", type=int, default=10)
    p.add_argument("--min-trigger-usd", type=float, default=1000.0)
    p.add_argument("--bot", default=BOT_DEFAULT)
    args = p.parse_args()

    src = Path(args.input_dir)
    summaries = read_csv(src / "event_chain_summary.csv")
    windows = read_csv(src / "top_event_windows_chain_ordered.csv")

    eligible = [
        r for r in summaries
        if str(r.get("chain_relation")) == "ALL_BOTS_AFTER_TRIGGER"
        and fnum(r.get("trigger_usd")) >= args.min_trigger_usd
        and inum(r.get("resolved_trade_rows")) == inum(r.get("window_trade_rows"))
    ]
    eligible.sort(key=lambda r: fnum(r.get("trigger_usd")), reverse=True)
    selected = eligible[: args.top_events]
    selected_ids = {str(r.get("event_id") or "") for r in selected}

    by_event: Dict[str, List[Dict[str, str]]] = {}
    for row in windows:
        event_id = str(row.get("event_id") or "")
        if event_id in selected_ids:
            by_event.setdefault(event_id, []).append(row)
    for rows in by_event.values():
        rows.sort(key=row_chain_key)

    final_summary: List[Dict[str, Any]] = []
    for rank, base in enumerate(selected, start=1):
        event_id = str(base.get("event_id") or "")
        rows = by_event.get(event_id, [])
        trigger_rows = [r for r in rows if truthy(r.get("is_trigger"))]
        bot_rows = [
            r for r in rows
            if truthy(r.get("is_bot_trade"))
            or str(r.get("trader_address") or "").lower() == args.bot.lower()
        ]
        trigger = trigger_rows[0] if trigger_rows else {}

        unique_bot: Dict[str, Dict[str, str]] = {}
        for r in bot_rows:
            h = str(r.get("trade_tx_hash") or "")
            if h and h not in unique_bot:
                unique_bot[h] = r
        ordered_bots = sorted(unique_bot.values(), key=row_chain_key)
        first_bot = ordered_bots[0] if ordered_bots else {}

        trigger_slot = inum(trigger.get("chain_slot"))
        trigger_index = inum(trigger.get("chain_tx_index"))
        bot_slot = inum(first_bot.get("chain_slot"))
        bot_index = inum(first_bot.get("chain_tx_index"))
        same_slot = bool(trigger and first_bot and trigger_slot == bot_slot)
        slot_delta = bot_slot - trigger_slot if trigger and first_bot else ""
        tx_index_delta = bot_index - trigger_index if same_slot else ""
        intervening = max(0, int(tx_index_delta) - 1) if same_slot else ""

        final_summary.append({
            "confirmed_rank": rank,
            "candidate_rank": base.get("rank", ""),
            "category": base.get("category") or category(str(base.get("trigger_side") or "")),
            "symbol": base.get("symbol", ""),
            "mint": base.get("mint", ""),
            "trigger_usd": round(fnum(base.get("trigger_usd")), 6),
            "trigger_side": base.get("trigger_side", ""),
            "trigger_tx_hash": base.get("trigger_tx_hash", ""),
            "trigger_slot": trigger_slot if trigger else "",
            "trigger_tx_index": trigger_index if trigger else "",
            "first_bot_tx_hash": first_bot.get("trade_tx_hash", ""),
            "first_bot_slot": bot_slot if first_bot else "",
            "first_bot_tx_index": bot_index if first_bot else "",
            "same_slot": same_slot,
            "slot_delta": slot_delta,
            "tx_index_delta_same_slot": tx_index_delta,
            "intervening_transactions_same_slot": intervening,
            "bot_tx_count": len(ordered_bots),
            "bot_tx_hashes": " | ".join(str(r.get("trade_tx_hash") or "") for r in ordered_bots),
            "chain_relation": "CONFIRMED_TRIGGER_BEFORE_BOT",
            "window_trade_rows": len(rows),
            "event_id": event_id,
        })

    final_rank = {str(r["event_id"]): int(r["confirmed_rank"]) for r in final_summary}
    final_windows: List[Dict[str, Any]] = []
    for event in final_summary:
        event_id = str(event["event_id"])
        for seq, row in enumerate(by_event.get(event_id, []), start=1):
            out: Dict[str, Any] = {
                "confirmed_rank": final_rank[event_id],
                "confirmed_chain_sequence": seq,
            }
            out.update(row)
            final_windows.append(out)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "confirmed_events_summary.csv", final_summary)
    write_csv(out_dir / "confirmed_event_windows.csv", final_windows)

    lines: List[str] = []
    for cat in ("大额买入触发", "大额卖出触发"):
        lines.append(f"# {cat}")
        lines.append("")
        for event in [r for r in final_summary if r["category"] == cat]:
            event_id = str(event["event_id"])
            lines.append(
                f"## 事件 #{event['confirmed_rank']:02d} {event['symbol']} | 中心大单 ${event['trigger_usd']:,.2f}"
            )
            lines.append(f"- Mint: `{event['mint']}`")
            lines.append(
                f"- 中心大单: `{event['trigger_tx_hash']}` | slot:index=`{event['trigger_slot']}:{event['trigger_tx_index']}`"
            )
            lines.append(
                f"- 第一笔目标机器人: `{event['first_bot_tx_hash']}` | slot:index=`{event['first_bot_slot']}:{event['first_bot_tx_index']}`"
            )
            if event["same_slot"]:
                lines.append(
                    f"- 同 slot：大单后第 {event['tx_index_delta_same_slot']} 个交易位置出现机器人；中间有 {event['intervening_transactions_same_slot']} 笔链上交易。"
                )
            else:
                lines.append(f"- 跨 slot：slot 差 = {event['slot_delta']}")
            lines.append("")
            lines.append("| 顺序 | slot:index | 标注 | OKX时间ms | 方向 | 金额USD | TxHash |")
            lines.append("|---:|---|---|---:|---|---:|---|")
            for seq, row in enumerate(by_event.get(event_id, []), start=1):
                if truthy(row.get("is_trigger")):
                    mark = "**中心大单**"
                elif truthy(row.get("is_bot_trade")) or str(row.get("trader_address") or "").lower() == args.bot.lower():
                    mark = "**套利机器人 MRiYA4**"
                elif truthy(row.get("is_large_trade")):
                    mark = "其他大额交易"
                else:
                    mark = "普通交易"
                lines.append(
                    f"| {seq} | {row.get('chain_slot')}:{row.get('chain_tx_index')} | {mark} | "
                    f"{row.get('trade_timestamp_ms')} | {row.get('trade_side')} | {fnum(row.get('trade_usd')):.6f} | "
                    f"`{row.get('trade_tx_hash')}` |"
                )
            lines.append("")
    (out_dir / "confirmed_event_blocks.md").write_text("\n".join(lines), encoding="utf-8")

    print(
        f"candidate_events={len(summaries)} eligible_confirmed={len(eligible)} "
        f"selected={len(final_summary)} rows={len(final_windows)}"
    )
    for r in final_summary:
        spacing = (
            f"same_slot index_delta={r['tx_index_delta_same_slot']} intervening={r['intervening_transactions_same_slot']}"
            if r["same_slot"]
            else f"slot_delta={r['slot_delta']}"
        )
        print(
            f"#{r['confirmed_rank']} {r['symbol']} ${r['trigger_usd']:.2f} "
            f"trigger={r['trigger_slot']}:{r['trigger_tx_index']} "
            f"bot={r['first_bot_slot']}:{r['first_bot_tx_index']} {spacing}"
        )


if __name__ == "__main__":
    main()
