#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe clean large-SELL events for the corrected passive-capture hypothesis.

Strategy definition for a SELL shock in pool A:
  H0: large token sell into A -> token price in A falls.
  H1+: arbitrageurs buy token from A and sell token into other pool(s) B.
  Ours: rest a passive bid in a B that receives arb sell-flow; after fill, sell
        acquired token into the recovering A.

This script does NOT assume that any AMM swap can fill our passive limit order.
It first identifies the landed venue/pool structure and transfer directions so a
later simulator can distinguish economic spread from actual maker executability.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set

CASES = [
    {
        "name": "ZEC_clean_sell",
        "mint": "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS",
        "trigger": "4XPXpSmoMTsgnUckxJGat4rZZDCwgYuKVk8mtqiHtQiogphHTVVWBFkPjprQpQ6LwfygGwg9CB67PibtZPQw4SX2",
        "bot": "5CaUV6W374ysTtusiSXQNp7jNbhgxwDuwaaD4YYBwdiwAXPxWkjGWEoAjToRCy7w4KSf9YuzXQ9g2PPYr5dTiZoG",
    },
    {
        "name": "SLIM_clean_sell",
        "mint": "xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW",
        "trigger": "2Z5fgLBVCyaHjGuNsHamWdVKSaJMniGne8GiswwgHg87cWT4CznwEcjiVCyvTBMzZULddFnaZapJ9MkuQHHhRU3B",
        "bot": "5oK4st7Rc8TzmeGYWS8Qrn9MfpjNhZ9v44KoLwanVeWs2YYESbaDtvKrLJSzKtyVtN6vPzcfLZXw7RYQzqg2fBtz",
    },
]

BASIC = {
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
}


def rpc_url() -> str:
    v = os.getenv("HELIUS_RPC_URL", "").strip()
    if v:
        return v
    k = os.getenv("HELIUS_API_KEY", "").strip()
    if k:
        return f"https://mainnet.helius-rpc.com/?api-key={k}"
    raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")


def rpc(method: str, params: List[Any]) -> Any:
    payload = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(rpc_url(), data=payload, headers={"content-type":"application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def get_tx(sig: str) -> Dict[str, Any]:
    tx = rpc("getTransaction", [sig, {"commitment":"finalized","encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
    if tx is None:
        raise RuntimeError(f"transaction not found {sig}")
    return tx


def key_rows(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    out=[]
    for x in tx["transaction"]["message"].get("accountKeys") or []:
        if isinstance(x, str):
            out.append({"pubkey":x,"signer":False,"writable":False})
        else:
            out.append({"pubkey":str(x.get("pubkey") or ""),"signer":bool(x.get("signer")),"writable":bool(x.get("writable"))})
    return out


def program_ids(tx: Dict[str, Any]) -> Set[str]:
    ks=[x["pubkey"] for x in key_rows(tx)]
    out=set()
    def add(ix: Dict[str, Any]) -> None:
        p=ix.get("programId")
        if p:
            out.add(str(p)); return
        i=ix.get("programIdIndex")
        if isinstance(i,int) and 0 <= i < len(ks):
            out.add(ks[i])
    for ix in tx["transaction"]["message"].get("instructions") or []:
        if isinstance(ix,dict): add(ix)
    for grp in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in grp.get("instructions") or []:
            if isinstance(ix,dict): add(ix)
    return out


def get_multiple_meta(pubkeys: List[str]) -> Dict[str, Any]:
    vals=rpc("getMultipleAccounts", [pubkeys,{"encoding":"base64","commitment":"finalized"}]).get("value") or []
    out={}
    for p,v in zip(pubkeys,vals):
        if not v:
            out[p]=None; continue
        raw=base64.b64decode(v["data"][0])
        out[p]={"owner_program":str(v.get("owner") or ""),"data_len":len(raw),"executable":bool(v.get("executable"))}
    return out


def net_deltas(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    ks=[x["pubkey"] for x in key_rows(tx)]
    meta=tx.get("meta") or {}
    pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    agg=defaultdict(int); decs={}
    for i in set(pre)|set(post):
        a,b=pre.get(i,{}),post.get(i,{})
        ref=b or a
        owner=str(ref.get("owner") or "")
        mint=str(ref.get("mint") or "")
        dec=int(((ref.get("uiTokenAmount") or {}).get("decimals") or 0))
        def raw(v: Dict[str,Any]) -> int:
            return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
        agg[(owner,mint)] += raw(b)-raw(a)
        decs[(owner,mint)] = dec
    rows=[]
    for (owner,mint),d in agg.items():
        if d:
            rows.append({"owner":owner,"mint":mint,"delta_ui":d/(10**decs[(owner,mint)])})
    return rows


def parsed_transfers_by_outer(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows=[]
    for grp in (tx.get("meta") or {}).get("innerInstructions") or []:
        outer=int(grp.get("index",-1))
        transfers=[]
        for ix in grp.get("instructions") or []:
            if not isinstance(ix,dict): continue
            p=ix.get("parsed")
            if not isinstance(p,dict) or p.get("type") not in {"transfer","transferChecked"}: continue
            info=p.get("info") or {}
            ta=info.get("tokenAmount") or {}
            amount_raw=int(ta.get("amount") or info.get("amount") or 0)
            dec=ta.get("decimals")
            amount_ui=(amount_raw/(10**int(dec))) if dec is not None else None
            transfers.append({
                "source":str(info.get("source") or ""),
                "destination":str(info.get("destination") or ""),
                "authority":str(info.get("authority") or ""),
                "mint":str(info.get("mint") or ""),
                "amount_raw":amount_raw,
                "amount_ui":amount_ui,
            })
        if transfers:
            rows.append({"outer":outer,"transfers":transfers})
    return rows


def ledger_index(slot: int, sig: str) -> int:
    block=rpc("getBlock", [slot,{"commitment":"finalized","transactionDetails":"signatures","rewards":False,"maxSupportedTransactionVersion":0}])
    return (block.get("signatures") or []).index(sig)


def summarize_tx(sig: str, target_mint: str) -> Dict[str, Any]:
    tx=get_tx(sig)
    keys=key_rows(tx)
    pids=sorted(program_ids(tx))
    nonbasic=[p for p in pids if p not in BASIC]
    meta=get_multiple_meta([x["pubkey"] for x in keys])
    candidate=[]
    for k in keys:
        m=meta.get(k["pubkey"])
        if not m or not k["writable"] or k["signer"] or m["executable"]:
            continue
        if m["owner_program"] in nonbasic:
            candidate.append({**k,**m})
    deltas=net_deltas(tx)
    target=[d for d in deltas if d["mint"]==target_mint]
    return {
        "signature":sig,
        "slot":int(tx["slot"]),
        "index":ledger_index(int(tx["slot"]),sig),
        "fee_lamports":int((tx.get("meta") or {}).get("fee") or 0),
        "signers":[x["pubkey"] for x in keys if x["signer"]],
        "nonbasic_programs":nonbasic,
        "candidate_program_state_accounts":candidate,
        "target_mint_net_deltas":target,
        "all_net_token_deltas":deltas,
        "transfer_groups":parsed_transfers_by_outer(tx),
    }


def main() -> None:
    all_out=[]
    for case in CASES:
        trig=summarize_tx(case["trigger"],case["mint"])
        bot=summarize_tx(case["bot"],case["mint"])
        rec={**case,"trigger_tx":trig,"bot_tx":bot,
             "ledger_relation":"trigger_before_bot" if (trig["slot"],trig["index"]) < (bot["slot"],bot["index"]) else "NOT_trigger_before_bot"}
        all_out.append(rec)
        print(f"\n=== {case['name']} ===")
        print("ledger",trig["slot"],trig["index"],"->",bot["slot"],bot["index"])
        print("TRIGGER programs",trig["nonbasic_programs"])
        print("TRIGGER target deltas",trig["target_mint_net_deltas"])
        print("TRIGGER candidate states")
        for x in trig["candidate_program_state_accounts"]: print(" ",x)
        print("BOT programs",bot["nonbasic_programs"])
        print("BOT target deltas",bot["target_mint_net_deltas"])
        print("BOT candidate states")
        for x in bot["candidate_program_state_accounts"]: print(" ",x)
        print("BOT all token deltas")
        for x in bot["all_net_token_deltas"]: print(" ",x)
        print("BOT transfer groups")
        for x in bot["transfer_groups"]: print(" ",x)

    out=Path("research/output/passive_capture/event_probe.json")
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(all_out,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__ == "__main__":
    main()
