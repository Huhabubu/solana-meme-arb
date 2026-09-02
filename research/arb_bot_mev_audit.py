#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计“目标套利机器人先于候选大单”的事件，判断是否存在 MEV 结构。

输入：arb_bot_chain_order.py 的 event_chain_summary.csv。
仅审计 chain_relation=ALL_BOTS_BEFORE_TRIGGER 的候选。

方法：
1. 对机器人/候选大单附近 ±N slot 读取 getBlock(transactionDetails=accounts)，找出窗口内所有包含目标机器人地址的链上交易；
2. 对这些机器人交易和候选大单调用 getTransaction；
3. 判断是否触及同一 Mint、共享程序、共享非系统账户；
4. 输出按链上顺序排列的机器人活动以及一个保守分类：
   - POSSIBLE_SANDWICH_OR_ORDERFLOW：大单前后都有机器人交易触及 Mint，且与大单共享关键账户/程序；
   - PRE_ARBITRAGE_THEN_TRIGGER：只有大单前机器人交易，或前置交易已经是原子双边套利，缺乏后腿证据；
   - LIKELY_MISPAIRED：机器人与大单相距较远且无明显同池/同账户关系；
   - NEEDS_MANUAL_DECODE：证据不足。

注意：这是结构审计，不把“机器人先”自动解释成 MEV。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

BOT_DEFAULT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"

# 常见系统/基础程序，不把它们的重合作为“同池”证据。
IGNORED_PROGRAMS = {
    "11111111111111111111111111111111",  # System
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # ATA
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
    "SysvarInstructions1111111111111111111111111",
}


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


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    seen: Set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def rpc_call(rpc_url: str, method: str, params: List[Any], *, retries: int = 5, timeout: int = 45) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                rpc_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "solana-meme-arb-mev-audit/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("error"):
                raise RuntimeError(f"RPC {method}: {body['error']}")
            return body.get("result")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))
    raise RuntimeError(f"RPC {method} failed: {last}")


def resolve_rpc_url(cli: str) -> str:
    if cli:
        return cli
    env = os.getenv("HELIUS_RPC_URL", "").strip()
    if env:
        return env
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit("RPC missing")


def account_pubkey(x: Any) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return str(x.get("pubkey") or "")
    return ""


def message_account_keys(tx_result: Dict[str, Any]) -> List[str]:
    msg = ((tx_result.get("transaction") or {}).get("message") or {})
    keys = [account_pubkey(x) for x in (msg.get("accountKeys") or [])]
    # v0 loaded addresses are not part of message.accountKeys in json encoding.
    meta = tx_result.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    keys.extend(str(x) for x in (loaded.get("writable") or []))
    keys.extend(str(x) for x in (loaded.get("readonly") or []))
    return [x for x in keys if x]


def program_ids(tx_result: Dict[str, Any]) -> Set[str]:
    keys = message_account_keys(tx_result)
    msg = ((tx_result.get("transaction") or {}).get("message") or {})
    out: Set[str] = set()

    def add_ix(ix: Dict[str, Any]) -> None:
        pid = ix.get("programId")
        if pid:
            out.add(str(pid))
            return
        idx = ix.get("programIdIndex")
        if isinstance(idx, int) and 0 <= idx < len(keys):
            out.add(keys[idx])

    for ix in (msg.get("instructions") or []):
        if isinstance(ix, dict):
            add_ix(ix)
    meta = tx_result.get("meta") or {}
    for group in (meta.get("innerInstructions") or []):
        for ix in (group.get("instructions") or []):
            if isinstance(ix, dict):
                add_ix(ix)
    return {x for x in out if x}


def token_mints(tx_result: Dict[str, Any]) -> Set[str]:
    meta = tx_result.get("meta") or {}
    out: Set[str] = set()
    for key in ("preTokenBalances", "postTokenBalances"):
        for row in (meta.get(key) or []):
            mint = row.get("mint")
            if mint:
                out.add(str(mint))
    return out


def get_transaction(rpc_url: str, sig: str) -> Optional[Dict[str, Any]]:
    return rpc_call(
        rpc_url,
        "getTransaction",
        [sig, {"commitment": "finalized", "encoding": "json", "maxSupportedTransactionVersion": 0}],
    )


def scan_bot_txs_in_slots(rpc_url: str, bot: str, start_slot: int, end_slot: int) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for slot in range(start_slot, end_slot + 1):
        block = rpc_call(
            rpc_url,
            "getBlock",
            [slot, {"commitment": "finalized", "transactionDetails": "accounts", "rewards": False, "maxSupportedTransactionVersion": 0}],
        )
        if not block:
            continue
        txs = block.get("transactions") or []
        for idx, item in enumerate(txs):
            tx = item.get("transaction") or {}
            msg = tx.get("message") or {}
            keys = [account_pubkey(x) for x in (msg.get("accountKeys") or [])]
            if bot not in keys:
                continue
            sigs = tx.get("signatures") or []
            if not sigs:
                continue
            found.append({"slot": slot, "tx_index": idx, "signature": str(sigs[0])})
        time.sleep(0.01)
    found.sort(key=lambda x: (x["slot"], x["tx_index"]))
    return found


def useful_shared_accounts(a: Dict[str, Any], b: Dict[str, Any], bot: str, mint: str) -> Set[str]:
    aa = set(message_account_keys(a))
    bb = set(message_account_keys(b))
    ignore = set(IGNORED_PROGRAMS) | {bot, mint}
    return (aa & bb) - ignore


def classify(
    trigger_slot: int,
    bot_anchor_slot: int,
    before_rows: List[Dict[str, Any]],
    after_rows: List[Dict[str, Any]],
    max_shared_accounts: int,
    shared_programs: int,
) -> Tuple[str, str]:
    slot_gap = trigger_slot - bot_anchor_slot
    if before_rows and after_rows and max_shared_accounts > 0 and shared_programs > 0:
        return (
            "POSSIBLE_SANDWICH_OR_ORDERFLOW",
            "候选大单前后均发现目标机器人触及该 Mint 的交易，且与大单存在共享程序/账户；需进一步解码池与资产净变化。",
        )
    if slot_gap >= 3 and max_shared_accounts == 0:
        return (
            "LIKELY_MISPAIRED",
            f"前置机器人早于候选大单 {slot_gap} 个 slot，且未发现有意义的共享账户，更像独立套利后碰巧出现大单。",
        )
    if before_rows and not after_rows:
        return (
            "PRE_ARBITRAGE_THEN_TRIGGER",
            "只看到大单前的机器人活动，窗口内没有同 Mint 的机器人后腿；暂不满足典型 sandwich 结构。",
        )
    return (
        "NEEDS_MANUAL_DECODE",
        "链上顺序明确，但仅凭账户/程序重合不足以确定 MEV 类型，需要解析具体 DEX 池和每腿资产流。",
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", default="research/output/arb_bot_events/candidates/chain_order")
    p.add_argument("--output-dir", default="research/output/arb_bot_events/mev_audit")
    p.add_argument("--slot-radius", type=int, default=5)
    p.add_argument("--bot", default=BOT_DEFAULT)
    p.add_argument("--rpc-url", default="")
    args = p.parse_args()

    rpc_url = resolve_rpc_url(args.rpc_url)
    src = Path(args.input_dir)
    summaries = read_csv(src / "event_chain_summary.csv")
    targets = [r for r in summaries if r.get("chain_relation") == "ALL_BOTS_BEFORE_TRIGGER"]
    if not targets:
        print("No bot-before-trigger candidates")
        return

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_rows: List[Dict[str, Any]] = []
    activity_rows: List[Dict[str, Any]] = []

    for ev in targets:
        event_id = ev["event_id"]
        mint = ev["mint"]
        trigger_sig = ev["trigger_tx_hash"]
        trigger_slot = inum(ev["trigger_slot"])
        trigger_idx = inum(ev["trigger_tx_index"])
        first_bot_pos = str(ev.get("bot_slot_indexes") or "").split(" | ")[0]
        bot_anchor_slot = inum(first_bot_pos.split(":")[0])
        start_slot = max(0, min(trigger_slot, bot_anchor_slot) - args.slot_radius)
        end_slot = max(trigger_slot, bot_anchor_slot) + args.slot_radius

        print(f"AUDIT {event_id} slots={start_slot}..{end_slot}")
        trigger_tx = get_transaction(rpc_url, trigger_sig)
        if not trigger_tx:
            print("  trigger not found")
            continue
        trigger_programs = program_ids(trigger_tx)

        bot_chain = scan_bot_txs_in_slots(rpc_url, args.bot, start_slot, end_slot)
        before_mint: List[Dict[str, Any]] = []
        after_mint: List[Dict[str, Any]] = []
        max_shared_accounts = 0
        max_shared_programs = 0

        for item in bot_chain:
            sig = item["signature"]
            tx = get_transaction(rpc_url, sig)
            if not tx:
                continue
            mints = token_mints(tx)
            touches_mint = mint in mints
            shared_accounts = useful_shared_accounts(trigger_tx, tx, args.bot, mint)
            shared_program = (trigger_programs & program_ids(tx)) - IGNORED_PROGRAMS
            max_shared_accounts = max(max_shared_accounts, len(shared_accounts))
            max_shared_programs = max(max_shared_programs, len(shared_program))
            pos = (item["slot"], item["tx_index"])
            trigger_pos = (trigger_slot, trigger_idx)
            relation = "BEFORE_TRIGGER" if pos < trigger_pos else "AFTER_TRIGGER" if pos > trigger_pos else "SAME_TX"
            row = {
                "event_id": event_id,
                "symbol": ev.get("symbol", ""),
                "mint": mint,
                "trigger_usd": ev.get("trigger_usd", ""),
                "trigger_slot": trigger_slot,
                "trigger_tx_index": trigger_idx,
                "bot_slot": item["slot"],
                "bot_tx_index": item["tx_index"],
                "relation_to_trigger": relation,
                "bot_tx_hash": sig,
                "touches_target_mint": touches_mint,
                "shared_non_system_accounts": len(shared_accounts),
                "shared_accounts": " | ".join(sorted(shared_accounts)),
                "shared_program_count": len(shared_program),
                "shared_programs": " | ".join(sorted(shared_program)),
            }
            activity_rows.append(row)
            if touches_mint and relation == "BEFORE_TRIGGER":
                before_mint.append(row)
            if touches_mint and relation == "AFTER_TRIGGER":
                after_mint.append(row)
            time.sleep(0.01)

        cls, reason = classify(
            trigger_slot,
            bot_anchor_slot,
            before_mint,
            after_mint,
            max_shared_accounts,
            max_shared_programs,
        )
        audit_rows.append({
            "event_id": event_id,
            "symbol": ev.get("symbol", ""),
            "mint": mint,
            "trigger_usd": fnum(ev.get("trigger_usd")),
            "trigger_side": ev.get("trigger_side", ""),
            "trigger_tx_hash": trigger_sig,
            "trigger_slot": trigger_slot,
            "trigger_tx_index": trigger_idx,
            "original_bot_slot_indexes": ev.get("bot_slot_indexes", ""),
            "slot_gap_from_anchor_bot": trigger_slot - bot_anchor_slot,
            "scanned_start_slot": start_slot,
            "scanned_end_slot": end_slot,
            "bot_txs_in_slot_window": len(bot_chain),
            "bot_txs_touching_mint_before": len(before_mint),
            "bot_txs_touching_mint_after": len(after_mint),
            "max_shared_non_system_accounts": max_shared_accounts,
            "max_shared_programs": max_shared_programs,
            "mev_structure_class": cls,
            "reason": reason,
        })

    write_csv(out_dir / "mev_audit_summary.csv", audit_rows)
    write_csv(out_dir / "mev_bot_activity.csv", activity_rows)

    lines: List[str] = ["# MRiYA4 bot-before-trigger MEV 审计", ""]
    for ev in audit_rows:
        lines += [
            f"## {ev['event_id']} | {ev['symbol']} | ${ev['trigger_usd']:,.2f}",
            f"- 分类：**{ev['mev_structure_class']}**",
            f"- 候选大单：`{ev['trigger_tx_hash']}` @ `{ev['trigger_slot']}:{ev['trigger_tx_index']}`",
            f"- 与原机器人锚点 slot 差：{ev['slot_gap_from_anchor_bot']}",
            f"- ±slot 窗口内机器人交易：{ev['bot_txs_in_slot_window']}；同 Mint 前={ev['bot_txs_touching_mint_before']}，后={ev['bot_txs_touching_mint_after']}",
            f"- 最大共享非系统账户：{ev['max_shared_non_system_accounts']}；共享程序：{ev['max_shared_programs']}",
            f"- 解释：{ev['reason']}",
            "",
            "| slot:index | 相对大单 | 触及Mint | 共享账户数 | 共享程序数 | TxHash |",
            "|---|---|---|---:|---:|---|",
        ]
        rows = [r for r in activity_rows if r["event_id"] == ev["event_id"]]
        rows.sort(key=lambda r: (inum(r["bot_slot"]), inum(r["bot_tx_index"])))
        for r in rows:
            lines.append(
                f"| {r['bot_slot']}:{r['bot_tx_index']} | {r['relation_to_trigger']} | {r['touches_target_mint']} | "
                f"{r['shared_non_system_accounts']} | {r['shared_program_count']} | `{r['bot_tx_hash']}` |"
            )
        lines.append("")
    (out_dir / "mev_audit.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"events": len(audit_rows), "activity_rows": len(activity_rows)}, ensure_ascii=False))
    for r in audit_rows:
        print(
            f"{r['event_id']} class={r['mev_structure_class']} bot_window={r['bot_txs_in_slot_window']} "
            f"mint_before={r['bot_txs_touching_mint_before']} mint_after={r['bot_txs_touching_mint_after']} "
            f"shared_accounts={r['max_shared_non_system_accounts']} shared_programs={r['max_shared_programs']}"
        )


if __name__ == "__main__":
    main()
