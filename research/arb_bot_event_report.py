#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 arb_bot_event_study.py 的原始输出生成“以大额触发交易为中心”的标准化事件报表。

规则：
- event 的唯一中心是 (mint, trigger_tx_hash)，同一触发交易不因附近出现多个机器人 tx 而重复计数；
- 只保留 search/event window 完整、且发现时锚定机器人 tx 为双边 BUY+SELL 的候选；
- 候选按 trigger_usd 从大到小排序，默认取前 10 个；
- summary 一行一个触发事件，并聚合该 ±event window 内 MRiYA4... 的所有交易；
- windows 保留这 10 个事件窗口内的全部逐笔成交。
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

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
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def trigger_key(row: Dict[str, Any]) -> Tuple[str, str]:
    mint = str(row.get("mint") or "")
    h = str(row.get("trigger_tx_hash") or "")
    if h:
        return mint, h
    return mint, str(row.get("trigger_timestamp_ms") or "")


def main() -> None:
    p = argparse.ArgumentParser(description="生成触发交易中心的套利事件 Top-N 报表")
    p.add_argument("--input-dir", default="research/output/arb_bot_events")
    p.add_argument("--output-dir", default="research/output/arb_bot_events/top10")
    p.add_argument("--top-events", type=int, default=10)
    p.add_argument("--bot", default=BOT_DEFAULT)
    p.add_argument("--min-trigger-usd", type=float, default=0.0)
    args = p.parse_args()

    if args.top_events <= 0:
        raise SystemExit("--top-events must be > 0")

    src = Path(args.input_dir)
    summaries = read_csv(src / "events_summary.csv")
    windows = read_csv(src / "event_windows.csv")

    # 一个 trigger 只保留一次；优先保留 trigger 金额更高/事件窗口更完整的记录。
    unique: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in summaries:
        if not truthy(row.get("bot_two_sided")):
            continue
        if not truthy(row.get("search_window_complete")) or not truthy(row.get("event_window_complete")):
            continue
        if fnum(row.get("trigger_usd")) < args.min_trigger_usd:
            continue
        key = trigger_key(row)
        current = unique.get(key)
        if current is None or fnum(row.get("trigger_usd")) > fnum(current.get("trigger_usd")):
            unique[key] = row

    selected = sorted(unique.values(), key=lambda r: fnum(r.get("trigger_usd")), reverse=True)[: args.top_events]
    selected_ids = {str(r.get("event_id") or "") for r in selected}
    selected_windows = [r for r in windows if str(r.get("event_id") or "") in selected_ids]

    by_event: Dict[str, List[Dict[str, str]]] = {}
    for row in selected_windows:
        by_event.setdefault(str(row.get("event_id") or ""), []).append(row)

    normalized_summary: List[Dict[str, Any]] = []
    for rank, event in enumerate(selected, start=1):
        event_id = str(event.get("event_id") or "")
        rows = by_event.get(event_id, [])
        bot_rows = [r for r in rows if truthy(r.get("is_bot_trade")) or str(r.get("trader_address") or "").lower() == args.bot.lower()]
        bot_hashes: List[str] = []
        seen_hashes = set()
        for r in bot_rows:
            h = str(r.get("trade_tx_hash") or "")
            if h and h not in seen_hashes:
                seen_hashes.add(h)
                bot_hashes.append(h)
        bot_rel = [inum(r.get("relative_to_trigger_ms")) for r in bot_rows]
        normalized_summary.append({
            "rank": rank,
            "symbol": event.get("symbol", ""),
            "mint": event.get("mint", ""),
            "trigger_time": event.get("trigger_time", ""),
            "trigger_timestamp_ms": event.get("trigger_timestamp_ms", ""),
            "trigger_side": event.get("trigger_side", ""),
            "trigger_usd": round(fnum(event.get("trigger_usd")), 6),
            "trigger_tx_hash": event.get("trigger_tx_hash", ""),
            "window_trade_rows": len(rows),
            "bot_trade_rows": len(bot_rows),
            "bot_tx_count": len(bot_hashes),
            "bot_buy_usd_window": round(sum(fnum(r.get("trade_usd")) for r in bot_rows if str(r.get("trade_side")) == "BUY"), 6),
            "bot_sell_usd_window": round(sum(fnum(r.get("trade_usd")) for r in bot_rows if str(r.get("trade_side")) == "SELL"), 6),
            "first_bot_relative_ms": min(bot_rel) if bot_rel else "",
            "last_bot_relative_ms": max(bot_rel) if bot_rel else "",
            "bot_tx_hashes": " | ".join(bot_hashes),
            "event_id": event_id,
        })

    # 详细窗口按 Top-N 排名 + 相对时间排序。
    rank_by_id = {str(r["event_id"]): int(r["rank"]) for r in normalized_summary}
    detailed: List[Dict[str, Any]] = []
    for row in selected_windows:
        out: Dict[str, Any] = {"rank": rank_by_id.get(str(row.get("event_id") or ""), 0)}
        out.update(row)
        detailed.append(out)
    detailed.sort(key=lambda r: (int(r["rank"]), inum(r.get("trade_timestamp_ms")), str(r.get("trade_tx_hash") or "")))

    out_dir = Path(args.output_dir)
    write_csv(out_dir / "top_events_summary.csv", normalized_summary)
    write_csv(out_dir / "top_event_windows.csv", detailed)

    print(f"unique_trigger_events={len(unique)} selected={len(normalized_summary)} detailed_rows={len(detailed)}")
    for r in normalized_summary:
        print(
            f"#{r['rank']} {r['symbol']} trigger=${r['trigger_usd']:.2f} {r['trigger_side']} "
            f"bot_txs={r['bot_tx_count']} bot_buy=${r['bot_buy_usd_window']:.2f} "
            f"bot_sell=${r['bot_sell_usd_window']:.2f} rel={r['first_bot_relative_ms']}..{r['last_bot_relative_ms']}ms"
        )


if __name__ == "__main__":
    main()
