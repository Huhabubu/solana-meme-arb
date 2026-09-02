#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trace A recovery after observed SLIM arb sell-flow fills.

This is the corrected temporal version of the passive-capture hypothesis:
  1. A large SELL shocks pool A lower.
  2. Arb flow buys A and sells into B_j.
  3. Hypothetically we are passively filled in B_j.
  4. We do NOT force an immediate exit. We track every later landed change to A
     across subsequent slots and ask when selling the fixed acquired SLIM amount
     back into A would first turn profitable and where the best historical exit
     within the horizon occurs.

The output is opportunity research, not a deployable strategy: choosing the
historical best exit uses look-ahead. It is used only to test whether a temporal
window exists at all.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

getcontext().prec = 60

START_SLOT=443464889
END_SLOT=443464919  # ~30 slots of recovery horizon
A_POOL="8idN93ZBpdtMp4672aS4GGMDy7LdVWCCXH7FKFdMw9P4"
A_SLIM_VAULT="6FoSD24CM2MyadTwVUqgZQ17kXozfMa3DfusbnuqYduy"
A_SOL_VAULT="EDL73XTnmr56U4ohW5uXXh6LJwsQQdoRLragMYEWLGPn"
SLIM="xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
WSOL="So11111111111111111111111111111111111111112"
SOLUSD_REF=Decimal("101.75")

ENTRIES=[
    {"name":"B1_MRiYA4","slot":443464889,"index":161,"qty_slim_raw":2_836_841_021,"cost_usdc_raw":13_101_520},
    {"name":"B2_later_arb","slot":443464889,"index":735,"qty_slim_raw":109_189_601,"cost_usdc_raw":505_014},
]

COIN_DECIMALS_OFFSET=32; PC_DECIMALS_OFFSET=40
SWAP_FEE_NUMERATOR_OFFSET=176; SWAP_FEE_DENOMINATOR_OFFSET=184
NEED_TAKE_PNL_COIN_OFFSET=192; NEED_TAKE_PNL_PC_OFFSET=200
COIN_VAULT_OFFSET=336; PC_VAULT_OFFSET=368
COIN_MINT_OFFSET=400; PC_MINT_OFFSET=432
B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58encode(raw:bytes)->str:
    n=int.from_bytes(raw,"big"); out=""
    while n:
        n,r=divmod(n,58); out=B58[r]+out
    z=0
    for b in raw:
        if b==0:z+=1
        else:break
    return "1"*z+(out or "")
def u64(raw:bytes,off:int)->int:return int.from_bytes(raw[off:off+8],"little")
def pk(raw:bytes,off:int)->str:return b58encode(raw[off:off+32])
def rpc_url()->str:
    v=os.getenv("HELIUS_RPC_URL","").strip()
    if v:return v
    k=os.getenv("HELIUS_API_KEY","").strip()
    if k:return f"https://mainnet.helius-rpc.com/?api-key={k}"
    raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")
def rpc(method:str,params:List[Any])->Any:
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=urllib.request.Request(rpc_url(),data=body,headers={"content-type":"application/json"})
    with urllib.request.urlopen(req,timeout=120) as resp: out=json.loads(resp.read().decode())
    if out.get("error"):raise RuntimeError(f"{method}: {out['error']}")
    return out.get("result")
def load_static()->Dict[str,Any]:
    v=rpc("getAccountInfo",[A_POOL,{"encoding":"base64","commitment":"finalized"}])["value"]
    raw=base64.b64decode(v["data"][0])
    return {"coin_decimals":u64(raw,COIN_DECIMALS_OFFSET),"pc_decimals":u64(raw,PC_DECIMALS_OFFSET),
            "fee_num":u64(raw,SWAP_FEE_NUMERATOR_OFFSET),"fee_den":u64(raw,SWAP_FEE_DENOMINATOR_OFFSET),
            "need_take_pnl_coin":u64(raw,NEED_TAKE_PNL_COIN_OFFSET),"need_take_pnl_pc":u64(raw,NEED_TAKE_PNL_PC_OFFSET),
            "coin_vault":pk(raw,COIN_VAULT_OFFSET),"pc_vault":pk(raw,PC_VAULT_OFFSET),
            "coin_mint":pk(raw,COIN_MINT_OFFSET),"pc_mint":pk(raw,PC_MINT_OFFSET)}
def keys(tx:Dict[str,Any])->List[str]:
    out=[]
    for x in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []:
        out.append(str(x if isinstance(x,str) else x.get("pubkey") or ""))
    return out
def sig(tx:Dict[str,Any])->str:
    a=(tx.get("transaction") or {}).get("signatures") or []
    return str(a[0]) if a else ""
def signers(tx:Dict[str,Any])->List[str]:
    out=[]
    for x in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []:
        if isinstance(x,dict) and x.get("signer"):out.append(str(x.get("pubkey") or ""))
    return out
def token_balance(tx:Dict[str,Any],pubkey:str)->Optional[Tuple[int,int,int,str]]:
    ks=keys(tx)
    try:i=ks.index(pubkey)
    except ValueError:return None
    meta=tx.get("meta") or {}
    pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    a,b=pre.get(i,{}),post.get(i,{})
    ref=b or a
    if not ref:return None
    ta=ref.get("uiTokenAmount") or {}; dec=int(ta.get("decimals") or 0); mint=str(ref.get("mint") or "")
    def raw(v:Dict[str,Any])->int:return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
    return raw(a),raw(b),dec,mint
def ceil_fraction(v:int,n:int,d:int)->int:return (v*n+d-1)//d
def quote_out(inp:int,out:int,amt:int,fn:int,fd:int)->Dict[str,int]:
    fee=ceil_fraction(amt,fn,fd); net=amt-fee; q=out*net//(inp+net)
    return {"fee":fee,"net_in":net,"amount_out":q}
def ui(raw:int,dec:int)->Decimal:return Decimal(raw)/(Decimal(10)**dec)

def main()->None:
    static=load_static()
    if static["coin_mint"]!=SLIM or static["pc_mint"]!=WSOL or static["coin_vault"]!=A_SLIM_VAULT or static["pc_vault"]!=A_SOL_VAULT:
        raise RuntimeError(f"unexpected A state {static}")
    slim_dec=int(static["coin_decimals"]); sol_dec=int(static["pc_decimals"])
    slim_pnl=int(static["need_take_pnl_coin"]); sol_pnl=int(static["need_take_pnl_pc"])

    # Build all A-changing states from the trigger slot onward.
    states=[]; carried_slim=None; carried_sol=None
    for slot in range(START_SLOT,END_SLOT+1):
        block=rpc("getBlock",[slot,{"commitment":"finalized","transactionDetails":"full","encoding":"jsonParsed","rewards":False,"maxSupportedTransactionVersion":0}])
        if not block:continue
        bt=block.get("blockTime")
        for idx,tx in enumerate(block.get("transactions") or []):
            if slot==START_SLOT and idx<161:continue
            sb=token_balance(tx,A_SLIM_VAULT); qb=token_balance(tx,A_SOL_VAULT)
            if not sb and not qb:continue
            if not sb or not qb:raise RuntimeError(f"only one A vault present {slot}:{idx}")
            sp,sq,sd,sm=sb; qp,qq,qd,qm=qb
            if sm!=SLIM or qm!=WSOL or sd!=slim_dec or qd!=sol_dec:raise RuntimeError("vault metadata mismatch")
            if carried_slim is not None and (sp!=carried_slim or qp!=carried_sol):
                raise RuntimeError(f"A continuity gap {slot}:{idx}: carried {carried_slim}/{carried_sol}, pre {sp}/{qp}")
            carried_slim,carried_sol=sq,qq
            if sq==sp and qq==qp:continue
            flow="BUY_SLIM_FROM_A" if sq<sp and qq>qp else ("SELL_SLIM_INTO_A" if sq>sp and qq<qp else "OTHER")
            states.append({"slot":slot,"index":idx,"block_time":bt,"signature":sig(tx),"signers":";".join(signers(tx)),
                           "flow":flow,"slim_delta_raw":sq-sp,"sol_delta_raw":qq-qp,
                           "slim_post_raw":sq,"sol_post_raw":qq})

    # For each observed passive-fill entry, mark the A state after that tx as the first eligible exit,
    # then evaluate all future A-changing states.
    evaluations=[]; summaries=[]
    for e in ENTRIES:
        eligible=[s for s in states if (s["slot"],s["index"]) >= (e["slot"],e["index"])]
        cost=ui(int(e["cost_usdc_raw"]),6); qty=int(e["qty_slim_raw"])
        first_positive=None; best=None
        for s in eligible:
            q=quote_out(int(s["slim_post_raw"])-slim_pnl,int(s["sol_post_raw"])-sol_pnl,qty,int(static["fee_num"]),int(static["fee_den"]))
            exit_sol=ui(q["amount_out"],sol_dec); value=exit_sol*SOLUSD_REF; pnl=value-cost; pnl_bps=pnl/cost*Decimal(10000)
            row={**e,"exit_slot":s["slot"],"exit_index":s["index"],"exit_block_time":s["block_time"],"exit_tx":s["signature"],
                 "A_flow_at_state":s["flow"],"A_exit_sol":str(exit_sol),"break_even_solusd":str(cost/exit_sol),
                 "solusd_ref":str(SOLUSD_REF),"pnl_usd":str(pnl),"pnl_bps":str(pnl_bps)}
            evaluations.append(row)
            if pnl>0 and first_positive is None:first_positive=row
            if best is None or Decimal(row["pnl_bps"])>Decimal(best["pnl_bps"]):best=row
        summaries.append({"entry":e,"first_positive":first_positive,"best_within_horizon":best,"evaluated_A_states":len(eligible)})

    outdir=Path("research/output/passive_capture");outdir.mkdir(parents=True,exist_ok=True)
    (outdir/"slim_postfill_recovery.json").write_text(json.dumps({"horizon":{"start_slot":START_SLOT,"end_slot":END_SLOT,"solusd_ref":str(SOLUSD_REF)},"A_states":states,"entry_summaries":summaries},ensure_ascii=False,indent=2),encoding="utf-8")
    if evaluations:
        with (outdir/"slim_postfill_recovery.csv").open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(evaluations[0].keys()));w.writeheader();w.writerows(evaluations)

    print("# SLIM post-fill A recovery")
    print("A changing states",len(states),"slots",START_SLOT,"..",END_SLOT)
    for s in states:print("A",s["slot"],s["index"],s["flow"],"dSLIM",ui(s["slim_delta_raw"],slim_dec),"dSOL",ui(s["sol_delta_raw"],sol_dec))
    print("\n# Entry summaries @ SOLUSD",SOLUSD_REF)
    for x in summaries:
        e=x["entry"];best=x["best_within_horizon"];fp=x["first_positive"]
        print(e["name"],"states",x["evaluated_A_states"],"first_positive",fp)
        print(" best",best)

if __name__=="__main__":main()
