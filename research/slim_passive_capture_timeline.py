#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trace the full same-slot SLIM convergence sequence after the clean SELL shock.

For each transaction after the trigger, track realized reserve changes in:
  A = Raydium AMM v4 SLIM/WSOL (shocked pool)
  B = Orca legacy token-swap SLIM/USDC (first observed arb destination)

Whenever a landed transaction sells SLIM into B, compute an event-local economic
counterfactual: if a passive bid had absorbed that exact B sale at its observed
aggregate B execution price, what would an immediate exact-input taker sale of
that SLIM into A return using A's ledger state available after that transaction?

This does NOT prove maker execution on B. B is an AMM in this sample. Results are
therefore labeled economic counterfactuals, not executable resting-limit PnL.
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

SLOT=443464889
TRIGGER_INDEX=160
FIRST_BOT_INDEX=161
TRIGGER="2Z5fgLBVCyaHjGuNsHamWdVKSaJMniGne8GiswwgHg87cWT4CznwEcjiVCyvTBMzZULddFnaZapJ9MkuQHHhRU3B"
FIRST_BOT="5oK4st7Rc8TzmeGYWS8Qrn9MfpjNhZ9v44KoLwanVeWs2YYESbaDtvKrLJSzKtyVtN6vPzcfLZXw7RYQzqg2fBtz"
A_POOL="8idN93ZBpdtMp4672aS4GGMDy7LdVWCCXH7FKFdMw9P4"
A_AUTH="5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
B_POOL="8JPid6GtND2tU3A7x7GDfPPEWwS36rMtzF7YoHU44UoA"
B_AUTH="749y4fXb9SzqmrLEetQdui5iDucnNiMgCJ2uzc3y7cou"
SLIM="xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
WSOL="So11111111111111111111111111111111111111112"
USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLUSD_REF=Decimal("101.75")

COIN_DECIMALS_OFFSET=32; PC_DECIMALS_OFFSET=40
SWAP_FEE_NUMERATOR_OFFSET=176; SWAP_FEE_DENOMINATOR_OFFSET=184
NEED_TAKE_PNL_COIN_OFFSET=192; NEED_TAKE_PNL_PC_OFFSET=200
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
    payload=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=urllib.request.Request(rpc_url(),data=payload,headers={"content-type":"application/json"})
    with urllib.request.urlopen(req,timeout=120) as resp: body=json.loads(resp.read().decode())
    if body.get("error"): raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")

def load_a_static()->Dict[str,Any]:
    v=rpc("getAccountInfo",[A_POOL,{"encoding":"base64","commitment":"finalized"}])["value"]
    raw=base64.b64decode(v["data"][0])
    return {"coin_decimals":u64(raw,COIN_DECIMALS_OFFSET),"pc_decimals":u64(raw,PC_DECIMALS_OFFSET),
            "fee_num":u64(raw,SWAP_FEE_NUMERATOR_OFFSET),"fee_den":u64(raw,SWAP_FEE_DENOMINATOR_OFFSET),
            "need_take_pnl_coin":u64(raw,NEED_TAKE_PNL_COIN_OFFSET),"need_take_pnl_pc":u64(raw,NEED_TAKE_PNL_PC_OFFSET),
            "coin_mint":pk(raw,COIN_MINT_OFFSET),"pc_mint":pk(raw,PC_MINT_OFFSET)}

def account_keys(txwrap:Dict[str,Any])->List[str]:
    msg=(txwrap.get("transaction") or {}).get("message") or {}
    out=[]
    for x in msg.get("accountKeys") or []:
        out.append(str(x if isinstance(x,str) else x.get("pubkey") or ""))
    return out

def token_owner_mint(txwrap:Dict[str,Any],owner:str,mint:str)->Optional[Tuple[int,int,int]]:
    meta=txwrap.get("meta") or {}
    pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    pre_sum=post_sum=0; dec=None; found=False
    for i in set(pre)|set(post):
        a,b=pre.get(i,{}),post.get(i,{})
        ref=b or a
        if str(ref.get("owner") or "")!=owner or str(ref.get("mint") or "")!=mint:continue
        found=True; dec=int(((ref.get("uiTokenAmount") or {}).get("decimals") or 0))
        def raw(v:Dict[str,Any])->int:return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
        pre_sum+=raw(a); post_sum+=raw(b)
    return (pre_sum,post_sum,int(dec or 0)) if found else None

def signature(txwrap:Dict[str,Any])->str:
    sigs=(txwrap.get("transaction") or {}).get("signatures") or []
    return str(sigs[0]) if sigs else ""

def signers(txwrap:Dict[str,Any])->List[str]:
    msg=(txwrap.get("transaction") or {}).get("message") or {}
    out=[]
    for x in msg.get("accountKeys") or []:
        if isinstance(x,dict) and x.get("signer"):out.append(str(x.get("pubkey") or ""))
    return out

def ui(raw:int,dec:int)->Decimal:return Decimal(raw)/(Decimal(10)**dec)
def bps(new:Decimal,old:Decimal)->Decimal:return (new/old-Decimal(1))*Decimal(10000)
def ceil_fraction(v:int,n:int,d:int)->int:return (v*n+d-1)//d

def quote_out(inp_res:int,out_res:int,amount_in:int,fee_num:int,fee_den:int)->Dict[str,int]:
    fee=ceil_fraction(amount_in,fee_num,fee_den); net=amount_in-fee
    out=out_res*net//(inp_res+net)
    return {"fee":fee,"net_in":net,"amount_out":out}

def reserve_price(q_raw:int,b_raw:int,q_dec:int,b_dec:int)->Decimal:
    return ui(q_raw,q_dec)/ui(b_raw,b_dec)

def classify(base_delta:int,quote_delta:int,base_name:str,venue:str)->str:
    if base_delta>0 and quote_delta<0:return f"SELL_{base_name}_INTO_{venue}"
    if base_delta<0 and quote_delta>0:return f"BUY_{base_name}_FROM_{venue}"
    if base_delta==0 and quote_delta==0:return "NO_CHANGE"
    return "OTHER_FLOW"

def main()->None:
    static=load_a_static()
    if static["coin_mint"]==SLIM:
        slim_pnl=static["need_take_pnl_coin"]; sol_pnl=static["need_take_pnl_pc"]
        slim_dec=static["coin_decimals"]; sol_dec=static["pc_decimals"]
    elif static["pc_mint"]==SLIM:
        slim_pnl=static["need_take_pnl_pc"]; sol_pnl=static["need_take_pnl_coin"]
        slim_dec=static["pc_decimals"]; sol_dec=static["coin_decimals"]
    else:raise RuntimeError("A is not SLIM pair")
    usdc_dec=6

    block=rpc("getBlock",[SLOT,{"commitment":"finalized","transactionDetails":"full","encoding":"jsonParsed","rewards":False,"maxSupportedTransactionVersion":0}])
    txs=block.get("transactions") or []
    if len(txs)<=FIRST_BOT_INDEX:raise RuntimeError("block shorter than expected")
    if signature(txs[TRIGGER_INDEX])!=TRIGGER or signature(txs[FIRST_BOT_INDEX])!=FIRST_BOT:
        raise RuntimeError("ledger anchors mismatch")

    # Anchor A/B states from first bot pre/post.
    first=txs[FIRST_BOT_INDEX]
    a_s=token_owner_mint(first,A_AUTH,SLIM); a_q=token_owner_mint(first,A_AUTH,WSOL)
    b_s=token_owner_mint(first,B_AUTH,SLIM); b_q=token_owner_mint(first,B_AUTH,USDC)
    if not all([a_s,a_q,b_s,b_q]):raise RuntimeError("first bot missing A/B balances")
    assert a_s and a_q and b_s and b_q
    a_state=[a_s[1],a_q[1]]; b_state=[b_s[1],b_q[1]]
    a_pre_trigger_s=token_owner_mint(txs[TRIGGER_INDEX],A_AUTH,SLIM)
    a_pre_trigger_q=token_owner_mint(txs[TRIGGER_INDEX],A_AUTH,WSOL)
    if not a_pre_trigger_s or not a_pre_trigger_q:raise RuntimeError("trigger A balances missing")
    a_pre_price=reserve_price(a_pre_trigger_q[0]-sol_pnl,a_pre_trigger_s[0]-slim_pnl,sol_dec,slim_dec)

    rows=[]; capture=[]
    for idx in range(FIRST_BOT_INDEX,len(txs)):
        tx=txs[idx]; sig=signature(tx)
        asnap=token_owner_mint(tx,A_AUTH,SLIM); aqnap=token_owner_mint(tx,A_AUTH,WSOL)
        bsnap=token_owner_mint(tx,B_AUTH,SLIM); bqnap=token_owner_mint(tx,B_AUTH,USDC)

        a_changed=False; b_changed=False
        a_ds=a_dq=b_ds=b_dq=0
        a_pre_local=list(a_state); b_pre_local=list(b_state)
        if asnap and aqnap:
            a_ds=asnap[1]-asnap[0]; a_dq=aqnap[1]-aqnap[0]
            if a_ds or a_dq:
                a_changed=True
                # Verify our carried state before replacing it.
                if idx!=FIRST_BOT_INDEX and (a_state[0]!=asnap[0] or a_state[1]!=aqnap[0]):
                    raise RuntimeError(f"A state discontinuity at {idx}")
                a_state=[asnap[1],aqnap[1]]
        if bsnap and bqnap:
            b_ds=bsnap[1]-bsnap[0]; b_dq=bqnap[1]-bqnap[0]
            if b_ds or b_dq:
                b_changed=True
                if idx!=FIRST_BOT_INDEX and (b_state[0]!=bsnap[0] or b_state[1]!=bqnap[0]):
                    raise RuntimeError(f"B state discontinuity at {idx}")
                b_state=[bsnap[1],bqnap[1]]
        if not (a_changed or b_changed):continue

        a_price=reserve_price(a_state[1]-sol_pnl,a_state[0]-slim_pnl,sol_dec,slim_dec)
        b_price=reserve_price(b_state[1],b_state[0],usdc_dec,slim_dec)
        row={
            "index":idx,"signature":sig,"signers":";".join(signers(tx)),
            "A_flow":classify(a_ds,a_dq,"SLIM","A") if a_changed else "NO_CHANGE",
            "A_slim_delta":str(ui(a_ds,slim_dec)),"A_sol_delta":str(ui(a_dq,sol_dec)),
            "A_post_sol_per_slim":str(a_price),"A_vs_pre_trigger_bps":str(bps(a_price,a_pre_price)),
            "B_flow":classify(b_ds,b_dq,"SLIM","B") if b_changed else "NO_CHANGE",
            "B_slim_delta":str(ui(b_ds,slim_dec)),"B_usdc_delta":str(ui(b_dq,usdc_dec)),
            "B_post_usdc_per_slim":str(b_price),
        }
        rows.append(row)

        # Actual landed B sell flow = B receives SLIM and pays USDC.
        if b_changed and b_ds>0 and b_dq<0:
            qty=b_ds; cost=-b_dq
            avg=ui(cost,usdc_dec)/ui(qty,slim_dec)
            q=quote_out(a_state[0]-slim_pnl,a_state[1]-sol_pnl,qty,static["fee_num"],static["fee_den"])
            exit_sol=ui(q["amount_out"],sol_dec)
            exit_usd=exit_sol*SOLUSD_REF
            cost_usd=ui(cost,usdc_dec)
            pnl=exit_usd-cost_usd
            cap={
                "index":idx,"signature":sig,"signers":";".join(signers(tx)),
                "observed_B_sell_slim":str(ui(qty,slim_dec)),"observed_B_usdc_out":str(cost_usd),
                "observed_B_avg_usdc_per_slim":str(avg),
                "A_post_sol_per_slim":str(a_price),"A_vs_pre_trigger_bps":str(bps(a_price,a_pre_price)),
                "immediate_A_exit_sol":str(exit_sol),"break_even_SOLUSD":str(cost_usd/exit_sol),
                "SOLUSD_ref":str(SOLUSD_REF),"economic_exit_value_usd":str(exit_usd),
                "economic_pnl_usd":str(pnl),"economic_pnl_bps":str(pnl/cost_usd*Decimal(10000)),
                "maker_executability_proven":False,
            }
            capture.append(cap)

    outdir=Path("research/output/passive_capture"); outdir.mkdir(parents=True,exist_ok=True)
    (outdir/"slim_timeline.json").write_text(json.dumps({
        "slot":SLOT,"A_pool":A_POOL,"B_pool":B_POOL,"SOLUSD_reference":str(SOLUSD_REF),
        "maker_executability_proven":False,"timeline":rows,"B_sell_capture_counterfactuals":capture
    },ensure_ascii=False,indent=2),encoding="utf-8")
    headers=list(rows[0].keys()) if rows else []
    with (outdir/"slim_timeline.csv").open("w",encoding="utf-8-sig",newline="") as f:
        if headers:
            w=csv.DictWriter(f,fieldnames=headers); w.writeheader(); w.writerows(rows)
    cheaders=list(capture[0].keys()) if capture else []
    with (outdir/"slim_capture_opportunities.csv").open("w",encoding="utf-8-sig",newline="") as f:
        if cheaders:
            w=csv.DictWriter(f,fieldnames=cheaders); w.writeheader(); w.writerows(capture)

    print("# SLIM same-slot passive-capture timeline")
    print("relevant A/B transactions",len(rows),"B-sell opportunities",len(capture))
    for r in rows:
        print("TX",r["index"],r["A_flow"],"A_vs_pre",r["A_vs_pre_trigger_bps"],"|",r["B_flow"],"Bpost",r["B_post_usdc_per_slim"])
    print("\n# Event-local passive-capture counterfactuals @ SOLUSD",SOLUSD_REF)
    for c in capture:
        print("CAP",c["index"],"Bsell",c["observed_B_sell_slim"],"SLIM cost",c["observed_B_usdc_out"],
              "Aexit",c["immediate_A_exit_sol"],"SOL pnl",c["economic_pnl_usd"],"USD",c["economic_pnl_bps"],"bps",
              "breakevenSOL",c["break_even_SOLUSD"])

if __name__=="__main__":main()
