#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan follow-on RAY pool activity after the clean MRiYA4 transaction.

We scan the remainder of slot 443497206 after index 888 and select every
transaction whose account list contains either:
- Raydium AMM v4 RAY/USDC pool 6Umm...; or
- Raydium CLMM RAY/SOL pool 2AXX....

For each matching transaction we fetch full parsed metadata and record:
- ledger index, signer, pool(s) touched;
- historical vault pre/post deltas;
- AMM v4 pre/post marginal reserve-ratio price when available;
- CLMM SwapEvent post sqrt price/tick when available.

This directly tests whether additional arbitrage flow continues to move the
other RAY pool after the first MRiYA4 backrun.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, List, Set

getcontext().prec = 50

SLOT = 443497206
FIRST_BOT_INDEX = 888
AMM_POOL = "6UmmUiYoBjSrhakAobJw8BvkmJtDVxaeBtbt7rxWo1mg"
CLMM_POOL = "2AXXcN6oN9bBT5owwmTH53C7QHUXvhLeu718Kqt8rvY2"
AMM_RAY_VAULT = "FdmKUE4UMiJYFK5ogCngHzShuVKrFXBamPWcewDr31th"
AMM_USDC_VAULT = "Eqrhxd7bDUCH3MepKmdVkgwazXRzY6iHhEoBpY7yAohk"
CLMM_RAY_VAULT = "Be1CFyoPAr8aBGxpvCPD2LD21hdz2vjYNq8EcypnmgGD"
CLMM_SOL_VAULT = "9Jgp8NpqEDFd5d3RQPfuRY7gMgRFByTNFmi68Ph1yvVb"
RAY = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
SWAP_EVENT_DISC = hashlib.sha256(b"event:SwapEvent").digest()[:8]
Q64 = Decimal(2) ** 64
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def rpc_url() -> str:
    v = os.getenv("HELIUS_RPC_URL", "").strip()
    if v:
        return v
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")


def rpc(method: str, params: List[Any]) -> Any:
    payload = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(rpc_url(), data=payload, headers={"content-type":"application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    zeros = 0
    for b in raw:
        if b == 0:
            zeros += 1
        else:
            break
    return "1" * zeros + (out or "")


def account_pubkey(x: Any) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return str(x.get("pubkey") or "")
    return ""


def get_tx(sig: str) -> Dict[str, Any]:
    tx = rpc("getTransaction", [sig, {
        "commitment":"finalized","encoding":"jsonParsed","maxSupportedTransactionVersion":0
    }])
    if not tx:
        raise RuntimeError(f"tx not found {sig}")
    return tx


def tx_keys(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = tx["transaction"]["message"].get("accountKeys") or []
    out = []
    for x in raw:
        if isinstance(x, str):
            out.append({"pubkey":x,"signer":False})
        else:
            out.append({"pubkey":str(x.get("pubkey") or ""),"signer":bool(x.get("signer"))})
    return out


def token_rows(tx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    keys = [x["pubkey"] for x in tx_keys(tx)]
    meta = tx.get("meta") or {}
    pre = {int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post = {int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    out: Dict[str, Dict[str, Any]] = {}
    for i in set(pre) | set(post):
        a,b = pre.get(i,{}),post.get(i,{})
        ref=b or a
        ui=ref.get("uiTokenAmount") or {}
        dec=int(ui.get("decimals") or 0)
        def raw(v: Dict[str,Any]) -> int:
            return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
        pre_raw,post_raw=raw(a),raw(b)
        out[keys[i]]={
            "mint":str(ref.get("mint") or ""),"decimals":dec,
            "pre_raw":pre_raw,"post_raw":post_raw,"delta_raw":post_raw-pre_raw,
            "pre_ui":str(Decimal(pre_raw)/(Decimal(10)**dec)),
            "post_ui":str(Decimal(post_raw)/(Decimal(10)**dec)),
            "delta_ui":str(Decimal(post_raw-pre_raw)/(Decimal(10)**dec)),
        }
    return out


def decode_clmm_events(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    events=[]
    for log in (tx.get("meta") or {}).get("logMessages") or []:
        if not log.startswith("Program data: "):
            continue
        raw=base64.b64decode(log.split(": ",1)[1])
        if len(raw)<205 or raw[:8]!=SWAP_EVENT_DISC:
            continue
        off=8
        pubs=[]
        for _ in range(4):
            pubs.append(b58encode(raw[off:off+32])); off+=32
        vals=[]
        for _ in range(4):
            vals.append(int.from_bytes(raw[off:off+8],"little")); off+=8
        zero=bool(raw[off]); off+=1
        sqrt=int.from_bytes(raw[off:off+16],"little"); off+=16
        liq=int.from_bytes(raw[off:off+16],"little"); off+=16
        tick=int.from_bytes(raw[off:off+4],"little",signed=True); off+=4
        fee0=int.from_bytes(raw[off:off+8],"little") if len(raw)>=off+8 else 0; off+=8
        fee1=int.from_bytes(raw[off:off+8],"little") if len(raw)>=off+8 else 0
        if pubs[0] == CLMM_POOL:
            events.append({
                "pool_state":pubs[0],"sender":pubs[1],"zero_for_one":zero,
                "amount0_raw":vals[0],"amount1_raw":vals[2],"sqrt_price_x64":sqrt,
                "liquidity":liq,"tick":tick,"trade_fee0_raw":fee0,"trade_fee1_raw":fee1,
            })
    return events


def clmm_sol_per_ray(sqrt: int) -> Decimal:
    # token0=WSOL(9), token1=RAY(6): RAY/SOL=(sqrt/Q64)^2*1000
    ray_per_sol=(Decimal(sqrt)/Q64)**2*Decimal(1000)
    return Decimal(1)/ray_per_sol


def amm_price(rows: Dict[str, Dict[str, Any]], side: str) -> Decimal | None:
    r=rows.get(AMM_RAY_VAULT)
    u=rows.get(AMM_USDC_VAULT)
    if not r or not u:
        return None
    ray=Decimal(r[f"{side}_ui"])
    usdc=Decimal(u[f"{side}_ui"])
    if ray==0:
        return None
    return usdc/ray


def main() -> None:
    block=rpc("getBlock", [SLOT, {
        "commitment":"finalized","transactionDetails":"accounts","rewards":False,"maxSupportedTransactionVersion":0
    }])
    candidates=[]
    for idx,item in enumerate(block.get("transactions") or []):
        if idx <= FIRST_BOT_INDEX:
            continue
        tx=item.get("transaction") or {}
        raw_keys=tx.get("accountKeys")
        if raw_keys is None:
            raw_keys=(tx.get("message") or {}).get("accountKeys") or []
        keys={account_pubkey(x) for x in raw_keys}
        touched=[]
        if AMM_POOL in keys:
            touched.append("AMM_V4_RAY_USDC")
        if CLMM_POOL in keys:
            touched.append("CLMM_RAY_SOL")
        if not touched:
            continue
        sigs=tx.get("signatures") or []
        if sigs:
            candidates.append({"index":idx,"signature":str(sigs[0]),"touched":touched})

    detailed=[]
    for c in candidates:
        tx=get_tx(c["signature"])
        keys=tx_keys(tx)
        signers=[x["pubkey"] for x in keys if x["signer"]]
        rows=token_rows(tx)
        rec={
            **c,"signers":signers,"fee_lamports":int((tx.get("meta") or {}).get("fee") or 0),
            "amm":None,"clmm_events":decode_clmm_events(tx),
        }
        if "AMM_V4_RAY_USDC" in c["touched"]:
            pre=amm_price(rows,"pre")
            post=amm_price(rows,"post")
            ray=rows.get(AMM_RAY_VAULT)
            usdc=rows.get(AMM_USDC_VAULT)
            rec["amm"]={
                "pre_usdc_per_ray":str(pre) if pre is not None else None,
                "post_usdc_per_ray":str(post) if post is not None else None,
                "move_bps":str((post/pre-1)*Decimal(10000)) if pre and post else None,
                "ray_vault_delta_ui":ray.get("delta_ui") if ray else None,
                "usdc_vault_delta_ui":usdc.get("delta_ui") if usdc else None,
            }
        for ev in rec["clmm_events"]:
            ev["post_sol_per_ray"]=str(clmm_sol_per_ray(ev["sqrt_price_x64"]))
        detailed.append(rec)

    out={
        "slot":SLOT,"first_bot_index":FIRST_BOT_INDEX,
        "candidate_count":len(detailed),"transactions":detailed,
    }
    path=Path("research/output/ray_clean_event_reconstruct/followon_pool_activity.json")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")

    print(f"follow-on pool transactions after {FIRST_BOT_INDEX}: {len(detailed)}")
    for r in detailed:
        print(f"index={r['index']} touched={','.join(r['touched'])} signer={(r['signers'] or [''])[0]} hash={r['signature']}")
        if r["amm"]:
            print("  AMM",r["amm"])
        for ev in r["clmm_events"]:
            print("  CLMM",{k:ev[k] for k in ("sender","zero_for_one","amount0_raw","amount1_raw","tick","post_sol_per_ray")})


if __name__=="__main__":
    main()
