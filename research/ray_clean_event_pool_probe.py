#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Decode the clean RAY large-trade -> MRiYA4 arbitrage event.

Goal: identify the actual DEX programs / likely pool-state accounts and reconstruct
per-token-account balance deltas from the two authoritative Solana transactions.

This is intentionally a forensic probe, not a generic historical replayer yet.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

TRIGGER_DEFAULT = "27oKQ1VHcA5HyECpispRQqKS6wtvzsUXjY28B62sQ1833BccxJoMBvGZqqgQvwogBF353w8boENB3VTsJqBAZim2"
BOT_TX_DEFAULT = "5HffqQCAvsiTqMi4LwB8HJBhi96BNc1QnvNj1vVrXLZipTHX7NxeZp5EggUK3HSff4rxAf6cj92QqM4tutASRPvw"
TARGET_MINT_DEFAULT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
BOT_DEFAULT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"

BASIC_PROGRAMS = {
    "11111111111111111111111111111111": "System",
    "ComputeBudget111111111111111111111111111111": "ComputeBudget",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "ATA",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo",
}


def rpc_url() -> str:
    v = os.getenv("HELIUS_RPC_URL", "").strip()
    if v:
        return v
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")


def rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url(), data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())
    if out.get("error"):
        raise RuntimeError(f"{method}: {out['error']}")
    return out.get("result")


def get_tx(sig: str) -> Dict[str, Any]:
    tx = rpc("getTransaction", [sig, {
        "commitment": "finalized",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
    }])
    if not tx:
        raise RuntimeError(f"tx not found: {sig}")
    return tx


def account_keys(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    out = []
    for x in raw:
        if isinstance(x, str):
            out.append({"pubkey": x, "signer": False, "writable": False, "source": "transaction"})
        else:
            out.append({
                "pubkey": str(x.get("pubkey") or ""),
                "signer": bool(x.get("signer")),
                "writable": bool(x.get("writable")),
                "source": str(x.get("source") or "transaction"),
            })
    return out


def token_balances(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    meta = tx.get("meta") or {}
    pre = {int(x["accountIndex"]): x for x in (meta.get("preTokenBalances") or [])}
    post = {int(x["accountIndex"]): x for x in (meta.get("postTokenBalances") or [])}
    keys = account_keys(tx)
    rows = []
    for idx in sorted(set(pre) | set(post)):
        a, b = pre.get(idx, {}), post.get(idx, {})
        mint = str((b or a).get("mint") or "")
        owner = str((b or a).get("owner") or "")
        def amt(x: Dict[str, Any]) -> int:
            return int(((x.get("uiTokenAmount") or {}).get("amount") or "0"))
        pre_raw, post_raw = amt(a), amt(b)
        decimals = int((((b or a).get("uiTokenAmount") or {}).get("decimals") or 0))
        rows.append({
            "account_index": idx,
            "account": keys[idx]["pubkey"] if idx < len(keys) else "",
            "writable": keys[idx]["writable"] if idx < len(keys) else None,
            "mint": mint,
            "owner": owner,
            "decimals": decimals,
            "pre_raw": pre_raw,
            "post_raw": post_raw,
            "delta_raw": post_raw - pre_raw,
            "pre_ui": pre_raw / (10 ** decimals) if decimals >= 0 else None,
            "post_ui": post_raw / (10 ** decimals) if decimals >= 0 else None,
            "delta_ui": (post_raw - pre_raw) / (10 ** decimals) if decimals >= 0 else None,
        })
    return rows


def ix_program(ix: Dict[str, Any], keys: List[Dict[str, Any]]) -> str:
    if ix.get("programId"):
        return str(ix["programId"])
    idx = ix.get("programIdIndex")
    if isinstance(idx, int) and idx < len(keys):
        return keys[idx]["pubkey"]
    return ""


def instruction_rows(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    keys = account_keys(tx)
    msg = (tx.get("transaction") or {}).get("message") or {}
    meta = tx.get("meta") or {}
    rows: List[Dict[str, Any]] = []

    def add(scope: str, outer_idx: int, inner_idx: int, ix: Dict[str, Any]) -> None:
        pid = ix_program(ix, keys)
        accounts: List[str] = []
        for a in ix.get("accounts") or []:
            if isinstance(a, int) and a < len(keys):
                accounts.append(keys[a]["pubkey"])
            else:
                accounts.append(str(a))
        parsed = ix.get("parsed")
        rows.append({
            "scope": scope,
            "outer_index": outer_idx,
            "inner_index": inner_idx,
            "program_id": pid,
            "program_label": BASIC_PROGRAMS.get(pid, "DEX/other"),
            "accounts": accounts,
            "parsed": parsed,
            "data": ix.get("data"),
        })

    for i, ix in enumerate(msg.get("instructions") or []):
        if isinstance(ix, dict):
            add("outer", i, -1, ix)
    for group in meta.get("innerInstructions") or []:
        oi = int(group.get("index", -1))
        for j, ix in enumerate(group.get("instructions") or []):
            if isinstance(ix, dict):
                add("inner", oi, j, ix)
    return rows


def invoked_programs(tx: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for row in instruction_rows(tx):
        p = row["program_id"]
        if p and p not in seen:
            seen.add(p); out.append(p)
    return out


def account_metadata(pubkeys: Iterable[str]) -> Dict[str, Any]:
    ps = list(dict.fromkeys(x for x in pubkeys if x))
    result: Dict[str, Any] = {}
    for start in range(0, len(ps), 100):
        batch = ps[start:start+100]
        vals = rpc("getMultipleAccounts", [batch, {"encoding": "base64", "commitment": "finalized"}])
        for p, v in zip(batch, vals.get("value") or []):
            if not v:
                result[p] = None
            else:
                data = v.get("data") or ["", "base64"]
                import base64
                result[p] = {
                    "owner_program": v.get("owner"),
                    "executable": v.get("executable"),
                    "lamports": v.get("lamports"),
                    "data_len": len(base64.b64decode(data[0])) if data and data[0] else 0,
                }
    return result


def summarize(sig: str, label: str, target_mint: str, bot: str) -> Dict[str, Any]:
    tx = get_tx(sig)
    keys = account_keys(tx)
    balances = token_balances(tx)
    ixs = instruction_rows(tx)
    programs = invoked_programs(tx)
    meta_map = account_metadata([x["pubkey"] for x in keys])

    nonbasic_programs = [p for p in programs if p not in BASIC_PROGRAMS]
    candidate_state = []
    for k in keys:
        md = meta_map.get(k["pubkey"])
        if not k["writable"] or k["signer"] or not md:
            continue
        owner_program = md.get("owner_program")
        if owner_program in nonbasic_programs:
            candidate_state.append({**k, **md})

    by_owner_mint = defaultdict(float)
    for b in balances:
        by_owner_mint[(b["owner"], b["mint"])] += float(b["delta_ui"] or 0)

    return {
        "label": label,
        "signature": sig,
        "slot": tx.get("slot"),
        "block_time": tx.get("blockTime"),
        "fee_lamports": (tx.get("meta") or {}).get("fee"),
        "err": (tx.get("meta") or {}).get("err"),
        "target_mint": target_mint,
        "bot": bot,
        "invoked_programs": [{"program_id": p, "label": BASIC_PROGRAMS.get(p, "DEX/other")} for p in programs],
        "nonbasic_programs": nonbasic_programs,
        "candidate_program_owned_writable_state_accounts": candidate_state,
        "token_balance_deltas": balances,
        "owner_mint_net_deltas": [
            {"owner": o, "mint": m, "delta_ui": d}
            for (o, m), d in sorted(by_owner_mint.items()) if abs(d) > 1e-18
        ],
        "instructions": ixs,
        "logs": (tx.get("meta") or {}).get("logMessages") or [],
        "accounts": [{**k, **(meta_map.get(k["pubkey"]) or {})} for k in keys],
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--trigger", default=TRIGGER_DEFAULT)
    p.add_argument("--bot-tx", default=BOT_TX_DEFAULT)
    p.add_argument("--mint", default=TARGET_MINT_DEFAULT)
    p.add_argument("--bot", default=BOT_DEFAULT)
    p.add_argument("--output", default="research/output/ray_clean_event_pool_probe.json")
    args = p.parse_args()

    trigger = summarize(args.trigger, "CENTER_LARGE_TRADE", args.mint, args.bot)
    arb = summarize(args.bot_tx, "MRiYA4_ARB", args.mint, args.bot)

    trigger_states = {x["pubkey"] for x in trigger["candidate_program_owned_writable_state_accounts"]}
    arb_states = {x["pubkey"] for x in arb["candidate_program_owned_writable_state_accounts"]}
    trigger_accounts = {x["pubkey"] for x in trigger["accounts"]}
    arb_accounts = {x["pubkey"] for x in arb["accounts"]}

    out = {
        "event": {
            "mint": args.mint,
            "trigger": args.trigger,
            "bot_tx": args.bot_tx,
            "ledger_relation": "443497206:887 -> 443497206:888",
        },
        "trigger": trigger,
        "arb": arb,
        "shared_candidate_state_accounts": sorted(trigger_states & arb_states),
        "shared_all_accounts": sorted(trigger_accounts & arb_accounts),
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"trigger slot={trigger['slot']} nonbasic_programs={trigger['nonbasic_programs']}")
    print("trigger candidate state:")
    for x in trigger["candidate_program_owned_writable_state_accounts"]:
        print(" ", x)
    print(f"arb slot={arb['slot']} nonbasic_programs={arb['nonbasic_programs']}")
    print("arb candidate state:")
    for x in arb["candidate_program_owned_writable_state_accounts"]:
        print(" ", x)
    print("shared candidate state:", out["shared_candidate_state_accounts"])
    print("\nTRIGGER net owner/mint deltas:")
    for x in trigger["owner_mint_net_deltas"]:
        print(x)
    print("\nARB net owner/mint deltas:")
    for x in arb["owner_mint_net_deltas"]:
        print(x)


if __name__ == "__main__":
    main()
