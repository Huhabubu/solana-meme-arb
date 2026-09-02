#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trace the full same-slot SLIM convergence sequence after the clean SELL shock.

For each transaction after the trigger, track exact vault changes in:
  A = Raydium AMM v4 SLIM/WSOL (shocked pool)
  B = Orca legacy token-swap SLIM/USDC (first observed arb destination)

Whenever a landed transaction sells SLIM into B, compute an event-local economic
counterfactual: if a passive bid had absorbed that exact B sale at its observed
aggregate B execution price, what would an immediate exact-input taker sale of
that SLIM into A return using A's ledger state available after that transaction?

Important: Raydium's 5Q544... authority is shared across AMM-v4 pools, so this
script intentionally tracks exact token-vault addresses rather than aggregating
balances by token-account owner.

This does NOT prove maker execution on B. B is an AMM in this sample. Results are
therefore economic counterfactuals, not executable resting-limit PnL.
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
B_POOL="8JPid6GtND2tU3A7x7GDfPPEWwS36rMtzF7YoHU44UoA"
SLIM="xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
WSOL="So11111111111111111111111111111111111111112"
USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Exact vaults identified from A AmmInfo and the landed B swap.
A_SLIM_VAULT="6FoSD24CM2MyadTwVUqgZQ17kXozfMa3DfusbnuqYduy"
A_SOL_VAULT="EDL73XTnmr56U4ohW5uXXh6LJwsQQdoRLragMYEWLGPn"
B_SLIM_VAULT="ErcxwkPgLdyoVL6j2SsekZ5iysPZEDRGfAggh282kQb8"
B_USDC_VAULT="EFYW6YEiCGpavuMPS1zoXhgfNkPisWkQ3bQz1b4UfKek"
SOLUSD_REF=Decimal("101.75")

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
            "coin_vault":pk(raw,COIN_VAULT_OFFSET),"pc_vault":pk(raw,PC_VAULT_OFFSET),
            "coin_mint":pk(raw,COIN_MINT_OFFSET),"pc_mint":pk(raw,PC_MINT_OFFSET)}

def account_keys(txwrap:Dict[str,Any])->List[str]:
    msg=(txwrap.get("transaction") or {}).get("message") or {}
    out=[]
    for x in msg.get("accountKeys") or []:
        out.append(str(x if isinstance(x,str) else x.get("pubkey") or ""))
    return out

def token_account_balance(txwrap:Dict[str,Any],pubkey:str)->Optional[Tuple[int,int,int,str]]:
    keys=account_keys(txwrap)
    try:i=keys.index(pubkey)
    except ValueError:return None
    meta=txwrap.get("meta") or {}
    pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    a,b=pre.get(i,{}),post.get(i,{})
    ref=b or a
    if not ref:return None
    ta=ref.get("uiTokenAmount") or {}; dec=int(ta.get("decimals") or 0); mint=str(ref.get("mint") or "")
    def raw(v:Dict[str,Any])->int:return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
    return raw(a),raw(b),dec,mint

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

def apply_snapshot(current:int,snap:Optional[Tuple[int,int,int,str]],idx:int,label:str)->Tuple[int,int,bool]:
    if snap is None:return current,0,False
    pre,post,_,_=snap
    if pre!=current:raise RuntimeError(f"{label} vault state discontinuity at {idx}: carried={current} tx_pre={pre}")
    return post,post-pre,(post!=pre)

def main()->None:
    static=load_a_static()
    if static["coin_vault"]!=A_SLIM_VAULT or static["pc_vault"]!=A_SOL_VAULT:
        raise RuntimeError(f"A vault mapping changed: {static}")
    if static["coin_mint"]!=SLIM or static["pc_mint"]!=WSOL:
        raise RuntimeError(f"unexpected A pair: {static}")
    slim_dec=int(static["coin_decimals"]); sol_dec=int(static["pc_decimals"]); usdc_dec=6
    slim_pnl=int(static["need_take_pnl_coin"]); sol_pnl=int(static["need_take_pnl_pc"])

    block=rpc("getBlock",[SLOT,{"commitment":"finalized","transactionDetails":"full","encoding":"jsonParsed","rewards":False,"maxSupportedTransactionVersion":0}])
    txs=block.get("transactions") or []
    if len(txs)<=FIRST_BOT_INDEX:raise RuntimeError("block shorter than expected")
    if signature(txs[TRIGGER_INDEX])!=TRIGGER or signature(txs[FIRST_BOT_INDEX])!=FIRST_BOT:
        raise RuntimeError("ledger anchors mismatch")

    trigger=txs[TRIGGER_INDEX]; first=txs[FIRST_BOT_INDEX]
    t_as=token_account_balance(trigger,A_SLIM_VAULT); t_aq=token_account_balance(trigger,A_SOL_VAULT)
    f_as=token_account_balance(first,A_SLIM_VAULT); f_aq=token_account_balance(first,A_SOL_VAULT)
    f_bs=token_account_balance(first,B_SLIM_VAULT); f_bq=token_account_balance(first,B_USDC_VAULT)
    if not all([t_as,t_aq,f_as,f_aq,f_bs,f_bq]):raise RuntimeError("anchor vault balance metadata missing")
    assert t_as and t_aq and f_as and f_aq and f_bs and f_bq
    if t_as[3]!=SLIM or f_as[3]!=SLIM or f_bs[3]!=SLIM or t_aq[3]!=WSOL or f_aq[3]!=WSOL or f_bq[3]!=USDC:
        raise RuntimeError("vault mint mismatch")
    if t_as[1]!=f_as[0] or t_aq[1]!=f_aq[0]:raise RuntimeError("trigger->first bot A continuity mismatch")

    a_pre_price=reserve_price(t_aq[0]-sol_pnl,t_as[0]-slim_pnl,sol_dec,slim_dec)
    # Start immediately before index 161, then apply every exact-vault change in order.
    a_slim,a_sol=f_as[0],f_aq[0]
    b_slim,b_usdc=f_bs[0],f_bq[0]

    rows=[]; capture=[]
    for idx in range(FIRST_BOT_INDEX,len(txs)):
        tx=txs[idx]; sig=signature(tx)
        asnap=token_account_balance(tx,A_SLIM_VAULT); aqnap=token_account_balance(tx,A_SOL_VAULT)
        bsnap=token_account_balance(tx,B_SLIM_VAULT); bqnap=token_account_balance(tx,B_USDC_VAULT)

        a_slim_new,a_ds,a_sc=apply_snapshot(a_slim,asnap,idx,"A_SLIM")
        a_sol_new,a_dq,a_qc=apply_snapshot(a_sol,aqnap,idx,"A_SOL")
        b_slim_new,b_ds,b_sc=apply_snapshot(b_slim,bsnap,idx,"B_SLIM")
        b_usdc_new,b_dq,b_qc=apply_snapshot(b_usdc,bqnap,idx,"B_USDC")
        a_changed=a_sc or a_qc; b_changed=b_sc or b_qc
        if not (a_changed or b_changed):continue
        a_slim,a_sol=a_slim_new,a_sol_new; b_slim,b_usdc=b_slim_new,b_usdc_new

        a_price=reserve_price(a_sol-sol_pnl,a_slim-slim_pnl,sol_dec,slim_dec)
        b_price=reserve_price(b_usdc,b_slim,usdc_dec,slim_dec)
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

        if b_changed and b_ds>0 and b_dq<0:
            qty=b_ds; cost=-b_dq
            avg=ui(cost,usdc_dec)/ui(qty,slim_dec)
            q=quote_out(a_slim-slim_pnl,a_sol-sol_pnl,qty,static["fee_num"],static["fee_den"])
            exit_sol=ui(q["amount_out"],sol_dec); cost_usd=ui(cost,usdc_dec); exit_usd=exit_sol*SOLUSD_REF
            pnl=exit_usd-cost_usd
            capture.append({
                "index":idx,"signature":sig,"signers":";".join(signers(tx)),
                "observed_B_sell_slim":str(ui(qty,slim_dec)),"observed_B_usdc_out":str(cost_usd),
                "observed_B_avg_usdc_per_slim":str(avg),
                "A_post_sol_per_slim":str(a_price),"A_vs_pre_trigger_bps":str(bps(a_price,a_pre_price)),
                "immediate_A_exit_sol":str(exit_sol),"break_even_SOLUSD":str(cost_usd/exit_sol),
                "SOLUSD_ref":str(SOLUSD_REF),"economic_exit_value_usd":str(exit_usd),
                "economic_pnl_usd":str(pnl),"economic_pnl_bps":str(pnl/cost_usd*Decimal(10000)),
                "maker_executability_proven":False,
            })

    outdir=Path("research/output/passive_capture"); outdir.mkdir(parents=True,exist_ok=True)
    (outdir/"slim_timeline.json").write_text(json.dumps({
        "slot":SLOT,"A_pool":A_POOL,"B_pool":B_POOL,"A_vaults":[A_SLIM_VAULT,A_SOL_VAULT],
        "B_vaults":[B_SLIM_VAULT,B_USDC_VAULT],"SOLUSD_reference":str(SOLUSD_REF),
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
    print("relevant exact-vault transactions",len(rows),"B-sell opportunities",len(capture))
    for r in rows:
        print("TX",r["index"],r["A_flow"],"A_vs_pre",r["A_vs_pre_trigger_bps"],"|",r["B_flow"],"Bpost",r["B_post_usdc_per_slim"])
    print("\n# Event-local passive-capture counterfactuals @ SOLUSD",SOLUSD_REF)
    for c in capture:
        print("CAP",c["index"],"Bsell",c["observed_B_sell_slim"],"SLIM cost",c["observed_B_usdc_out"],
              "Aexit",c["immediate_A_exit_sol"],"SOL pnl",c["economic_pnl_usd"],"USD",c["economic_pnl_bps"],"bps",
              "breakevenSOL",c["break_even_SOLUSD"])

if __name__=="__main__":main()
