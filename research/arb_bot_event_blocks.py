#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把标准化套利事件输出整理成“以中心大额交易为核心”的事件块。

输入：arb_bot_event_report.py 生成的 top10/top_events_summary.csv 与 top10/top_event_windows.csv。
输出：
- event_timeline.csv：长表，一行一笔成交；最前面带事件分类/事件编号/中心大单信息；
- event_blocks.md：人类可读的事件块，每块内按时间顺序列出完整窗口交易。

分类：
- 大额买入触发：中心大单方向 BUY
- 大额卖出触发：中心大单方向 SELL

标注：
- 【中心大单】
- 【套利机器人 MRiYA4】
- 【其他大额交易】
- 普通交易
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

BOT_DEFAULT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes"}


def inum(v: Any) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def fnum(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def event_class(trigger_side: str) -> str:
    return "大额买入触发" if str(trigger_side).upper() == "BUY" else "大额卖出触发"


def record_type(row: Dict[str, str], bot: str) -> str:
    if truthy(row.get("is_trigger")):
        return "【中心大单】"
    if truthy(row.get("is_bot_trade")) or str(row.get("trader_address") or "").lower() == bot.lower():
        return "【套利机器人 MRiYA4】"
    if truthy(row.get("is_large_trade")):
        return "【其他大额交易】"
    return "普通交易"


def short_hash(h: str, n: int = 10) -> str:
    h = str(h or "")
    return h if len(h) <= n * 2 else f"{h[:n]}...{h[-n:]}"


def main() -> None:
    p = argparse.ArgumentParser(description="生成以中心大单为核心的套利事件块")
    p.add_argument("--input-dir", default="research/output/arb_bot_events/top10")
    p.add_argument("--output-dir", default="research/output/arb_bot_events/top10/blocks")
    p.add_argument("--bot", default=BOT_DEFAULT)
    args = p.parse_args()

    src = Path(args.input_dir)
    summaries = read_csv(src / "top_events_summary.csv")
    windows = read_csv(src / "top_event_windows.csv")
    by_event: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in windows:
        by_event[str(row.get("event_id") or "")].append(row)
    for rows in by_event.values():
        rows.sort(key=lambda r: (inum(r.get("trade_timestamp_ms")), str(r.get("trade_tx_hash") or ""), str(r.get("trade_side") or "")))

    # 分类优先：买入触发在前、卖出触发在后；分类内沿原 rank。
    summaries.sort(key=lambda r: (0 if str(r.get("trigger_side")).upper() == "BUY" else 1, inum(r.get("rank"))))

    timeline: List[Dict[str, Any]] = []
    md: List[str] = [
        "# MRiYA4 套利事件块",
        "",
        f"目标套利机器人：`{args.bot}`",
        "",
        "每个事件以一笔中心大额交易为核心，完整保留其窗口内所有逐笔成交，并按时间顺序排列。",
        "",
    ]

    current_class = None
    event_no = 0
    for s in summaries:
        cls = event_class(str(s.get("trigger_side") or ""))
        if cls != current_class:
            md += [f"## {cls}", ""]
            current_class = cls
        event_no += 1
        event_id = str(s.get("event_id") or "")
        rows = by_event.get(event_id, [])
        trigger_hash = str(s.get("trigger_tx_hash") or "")
        trigger_usd = fnum(s.get("trigger_usd"))

        md += [
            f"### 事件 {event_no:02d} | {s.get('symbol','')} | {cls}",
            "",
            f"- Mint：`{s.get('mint','')}`",
            f"- 中心大单：{s.get('trigger_side','')} ${trigger_usd:,.2f}",
            f"- 中心时间：{s.get('trigger_time','')}",
            f"- 中心 TxHash：`{trigger_hash}`",
            f"- 窗口成交：{len(rows)} 条",
            "",
            "| 序号 | 时间 | 相对中心ms | 标注 | 方向 | 金额USD | 价格 | 地址 | TxHash |",
            "|---:|---|---:|---|:---:|---:|---:|---|---|",
        ]

        for seq, row in enumerate(rows, start=1):
            tag = record_type(row, args.bot)
            out = {
                "事件分类": cls,
                "事件编号": event_no,
                "原始排名": inum(s.get("rank")),
                "Token": s.get("symbol", ""),
                "Mint": s.get("mint", ""),
                "中心大单方向": s.get("trigger_side", ""),
                "中心大单金额USD": round(trigger_usd, 6),
                "中心大单时间": s.get("trigger_time", ""),
                "中心大单TxHash": trigger_hash,
                "事件内序号": seq,
                "交易时间": row.get("trade_time", ""),
                "相对中心ms": inum(row.get("relative_to_trigger_ms")),
                "记录标注": tag,
                "交易方向": row.get("trade_side", ""),
                "交易金额USD": round(fnum(row.get("trade_usd")), 6),
                "交易价格": fnum(row.get("trade_price")),
                "交易者地址": row.get("trader_address", ""),
                "交易TxHash": row.get("trade_tx_hash", ""),
            }
            timeline.append(out)
            md.append(
                f"| {seq} | {row.get('trade_time','')} | {inum(row.get('relative_to_trigger_ms'))} | {tag} | "
                f"{row.get('trade_side','')} | ${fnum(row.get('trade_usd')):,.2f} | {fnum(row.get('trade_price')):.12g} | "
                f"`{short_hash(str(row.get('trader_address') or ''))}` | `{short_hash(str(row.get('trade_tx_hash') or ''))}` |"
            )
        md.append("")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "event_timeline.csv", timeline)
    (out_dir / "event_blocks.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"events={event_no} timeline_rows={len(timeline)}")
    print(f"timeline={out_dir / 'event_timeline.csv'}")
    print(f"blocks={out_dir / 'event_blocks.md'}")


if __name__ == "__main__":
    main()
