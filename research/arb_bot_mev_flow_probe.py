#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 MEV 候选中目标机器人在目标 Mint 上的单笔交易净持仓变化。"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BOT_DEFAULT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"


def rpc_call(url: str, method: str, params: List[Any]) -> Any:
    payload = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


def rpc_url(cli: str) -> str:
    if cli:
        return cli
    if os.getenv("HELIUS_RPC_URL"):
        return os.environ["HELIUS_RPC_URL"]
    key = os.getenv("HELIUS_API_KEY", "")
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit("RPC missing")


def token_amount(row: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    if not row:
        return 0, 0
    ui = row.get("uiTokenAmount") or {}
    return int(ui.get("amount") or 0), int(ui.get("decimals") or 0)


def bot_mint_delta(tx: Dict[str, Any], bot: str, mint: str) -> Dict[str, Any]:
    meta = tx.get("meta") or {}
    pre_rows = [r for r in (meta.get("preTokenBalances") or []) if str(r.get("mint")) == mint]
    post_rows = [r for r in (meta.get("postTokenBalances") or []) if str(r.get("mint")) == mint]
    pre = {int(r["accountIndex"]): r for r in pre_rows}
    post = {int(r["accountIndex"]): r for r in post_rows}
    indexes = sorted(set(pre) | set(post))
    raw_delta = 0
    decimals: Optional[int] = None
    matching_accounts = 0
    per_account = []
    for idx in indexes:
        a = pre.get(idx)
        b = post.get(idx)
        owner_a = str((a or {}).get("owner") or "")
        owner_b = str((b or {}).get("owner") or "")
        if bot not in {owner_a, owner_b}:
            continue
        pa, da = token_amount(a)
        pb, db = token_amount(b)
        d = db if b else da
        decimals = d if decimals is None else decimals
        delta = pb - pa
        raw_delta += delta
        matching_accounts += 1
        per_account.append({"account_index": idx, "pre_raw": pa, "post_raw": pb, "delta_raw": delta})
    ui_delta = None if decimals is None else raw_delta / (10 ** decimals)
    return {
        "bot_owned_target_accounts": matching_accounts,
        "target_mint_delta_raw": raw_delta,
        "target_mint_decimals": decimals,
        "target_mint_delta_ui": ui_delta,
        "target_mint_flat": raw_delta == 0 if decimals is not None else None,
        "per_account": per_account,
    }


def fetch_tx(url: str, sig: str) -> Dict[str, Any]:
    tx = rpc_call(url, "getTransaction", [sig, {"commitment":"finalized","encoding":"json","maxSupportedTransactionVersion":0}])
    if not tx:
        raise RuntimeError(f"tx not found {sig}")
    return tx


def main() -> None:
    p=argparse.ArgumentParser()
    p.add_argument("--cases-json", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--bot", default=BOT_DEFAULT)
    p.add_argument("--rpc-url", default="")
    args=p.parse_args()
    url=rpc_url(args.rpc_url)
    cases=json.loads(Path(args.cases_json).read_text(encoding="utf-8"))
    result=[]
    for case in cases:
        mint=case["mint"]
        rows=[]
        for item in case["transactions"]:
            tx=fetch_tx(url,item["tx_hash"])
            flow=bot_mint_delta(tx,args.bot,mint)
            rows.append({
                "label":item["label"],
                "relation":item["relation"],
                "slot":tx.get("slot"),
                "tx_hash":item["tx_hash"],
                **flow,
            })
        result.append({"case":case["case"],"mint":mint,"transactions":rows})
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    for case in result:
        print(f"CASE {case['case']}")
        for r in case["transactions"]:
            print(
                f"  {r['label']} {r['relation']} slot={r['slot']} flat={r['target_mint_flat']} "
                f"delta_ui={r['target_mint_delta_ui']} accounts={r['bot_owned_target_accounts']} {r['tx_hash']}"
            )

if __name__ == "__main__":
    main()
