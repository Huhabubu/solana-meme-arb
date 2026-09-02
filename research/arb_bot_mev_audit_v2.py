#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2: 修正 getBlock(transactionDetails='accounts') 的账户结构后运行 MEV 审计。"""
from __future__ import annotations

from typing import Any, Dict, List

import arb_bot_mev_audit as base


def _pubkey(x: Any) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return str(x.get("pubkey") or "")
    return ""


def scan_bot_txs_in_slots(rpc_url: str, bot: str, start_slot: int, end_slot: int) -> List[Dict[str, Any]]:
    """accounts 模式下 transaction.accountKeys 位于 transaction 根级，不在 message 下。"""
    found: List[Dict[str, Any]] = []
    for slot in range(start_slot, end_slot + 1):
        block = base.rpc_call(
            rpc_url,
            "getBlock",
            [
                slot,
                {
                    "commitment": "finalized",
                    "transactionDetails": "accounts",
                    "rewards": False,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if not block:
            continue
        for idx, item in enumerate(block.get("transactions") or []):
            tx = item.get("transaction") or {}
            # Solana accounts 模式：accountKeys 在 transaction 根级。
            raw_keys = tx.get("accountKeys")
            # 兼容某些 RPC 仍返回 message.accountKeys 的情况。
            if raw_keys is None:
                raw_keys = (tx.get("message") or {}).get("accountKeys") or []
            keys = [_pubkey(x) for x in (raw_keys or [])]
            if bot not in keys:
                continue
            sigs = tx.get("signatures") or []
            if not sigs:
                continue
            found.append({"slot": slot, "tx_index": idx, "signature": str(sigs[0])})
    found.sort(key=lambda x: (x["slot"], x["tx_index"]))
    return found


base.scan_bot_txs_in_slots = scan_bot_txs_in_slots

if __name__ == "__main__":
    base.main()
