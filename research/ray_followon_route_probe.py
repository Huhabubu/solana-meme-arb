#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Decode the five follow-on RAY/SOL CLMM-selling transactions after MRiYA4.

For each transaction, enumerate non-basic invoked programs, program-owned writable
state accounts, and token-owner net deltas. This identifies where follow-on
arbitrageurs sourced RAY before selling it into the shocked CLMM.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set

TXS = [
    (1166, "2K17cZCJDrrs4S3bsW9FD3nBZfMRDTtPprrd5RbUSdHZnw3sDkrmzcBodHyD3CKczU3EcR3uYkH7qNva73in1oWM"),
    (1174, "5QoSGDsnKLHLsxNTJ2PLUduCeZ8rz1hatBAQPKUyUE1AZp4ysrW6zJi5eu1ZKgarUxQDV5MoNwN4jviUm3hkgKQ9"),
    (1214, "8dLj5gVdy6oywa9eaVuZJp4ojWz5QmZm2ZGYt1CSpooV6anyJ3bLFST1As32qqQzZhc8Dhcq1iv3YhPbaGWSoFB"),
    (1219, "5rc1Qi4nw1J4MDWXD1sGbZZHocyLWCjzsUkkRgsM3xpUUwh2n41V6trZKG4Pn3TzZBht8LGNiiJ2g65Qhpb4pc3Z"),
    (1222, "52ChRxHEnvkyVqLqGqdijDSwx3WBQGnTfVhVjmjNB6JJuEDUviqNdhw6hvqrkokeGb98sUPsEwB3XhiM6ixfQvwz"),
]

BASIC = {
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
}
RAY="4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
WSOL="So11111111111111111111111111111111111111112"
USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CLMM="CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"


def rpc_url()->str:
    v=os.getenv("HELIUS_RPC_URL","").strip()
    if v:return v
    k=os.getenv("HELIUS_API_KEY","").strip()
    if k:return f"https://mainnet.helius-rpc.com/?api-key={k}"
    raise SystemExit("RPC missing")


def rpc(method:str,params:List[Any])->Any:
    payload=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=urllib.request.Request(rpc_url(),data=payload,headers={"content-type":"application/json"})
    with urllib.request.urlopen(req,timeout=90) as r: body=json.loads(r.read().decode())
    if body.get("error"):raise RuntimeError(body["error"])
    return body.get("result")


def get_tx(sig:str)->Dict[str,Any]:
    return rpc("getTransaction",[sig,{"commitment":"finalized","encoding":"jsonParsed","maxSupportedTransactionVersion":0}])


def keys(tx:Dict[str,Any])->List[Dict[str,Any]]:
    out=[]
    for x in tx["transaction"]["message"].get("accountKeys") or []:
        if isinstance(x,str):out.append({"pubkey":x,"signer":False,"writable":False})
        else:out.append({"pubkey":str(x.get("pubkey") or ""),"signer":bool(x.get("signer")),"writable":bool(x.get("writable"))})
    return out


def program_ids(tx:Dict[str,Any])->Set[str]:
    ks=[x["pubkey"] for x in keys(tx)]
    out=set()
    def add(ix:Dict[str,Any]):
        p=ix.get("programId")
        if p:out.add(str(p));return
        i=ix.get("programIdIndex")
        if isinstance(i,int) and i<len(ks):out.add(ks[i])
    for ix in tx["transaction"]["message"].get("instructions") or []:
        if isinstance(ix,dict):add(ix)
    for g in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in g.get("instructions") or []:
            if isinstance(ix,dict):add(ix)
    return out


def multi_meta(pubkeys:List[str])->Dict[str,Any]:
    vals=rpc("getMultipleAccounts",[pubkeys,{"encoding":"base64","commitment":"finalized"}]).get("value") or []
    out={}
    for p,v in zip(pubkeys,vals):
        if not v:out[p]=None;continue
        raw=base64.b64decode(v["data"][0])
        out[p]={"owner_program":v.get("owner"),"data_len":len(raw),"executable":bool(v.get("executable"))}
    return out


def net_deltas(tx:Dict[str,Any])->List[Dict[str,Any]]:
    ks=[x["pubkey"] for x in keys(tx)]
    meta=tx.get("meta") or {}
    pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    agg=defaultdict(lambda:0)
    decs={}
    for i in set(pre)|set(post):
        a,b=pre.get(i,{}),post.get(i,{})
        ref=b or a
        mint=str(ref.get("mint") or "");owner=str(ref.get("owner") or "")
        ui=ref.get("uiTokenAmount") or {};dec=int(ui.get("decimals") or 0)
        def raw(v):return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
        agg[(owner,mint)]+=raw(b)-raw(a);decs[(owner,mint)]=dec
    rows=[]
    for (owner,mint),delta in agg.items():
        if delta:
            dec=decs[(owner,mint)]
            rows.append({"owner":owner,"mint":mint,"delta_ui":delta/(10**dec)})
    return rows


def main()->None:
    out=[]
    for idx,sig in TXS:
        tx=get_tx(sig)
        ks=keys(tx);ps=sorted(program_ids(tx));nonbasic=[p for p in ps if p not in BASIC]
        meta=multi_meta([x["pubkey"] for x in ks])
        candidates=[]
        for k in ks:
            m=meta.get(k["pubkey"])
            if not k["writable"] or k["signer"] or not m:continue
            if m["owner_program"] in nonbasic and not m["executable"]:
                candidates.append({**k,**m})
        signers=[x["pubkey"] for x in ks if x["signer"]]
        rec={"index":idx,"tx_hash":sig,"signers":signers,"nonbasic_programs":nonbasic,
             "candidate_program_state_accounts":candidates,"net_token_deltas":net_deltas(tx)}
        out.append(rec)
        print("\n===",idx,sig,"===")
        print("signers",signers)
        print("programs",nonbasic)
        print("candidate states")
        for c in candidates:print(" ",c)
        print("net deltas")
        for d in rec["net_token_deltas"]:
            if d["mint"] in {RAY,WSOL,USDC}:print(" ",d)
    p=Path("research/output/ray_clean_event_reconstruct/followon_route_probe.json")
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__":main()
