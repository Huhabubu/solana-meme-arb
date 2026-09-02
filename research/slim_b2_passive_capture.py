#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reconstruct the later SLIM B2 passive-capture counterfactual at index 735.

Known landed route at slot 443464889 index 735:
  0.500000 USDC -> 0.004912405 SOL -> buy 109.189601 SLIM from shocked A
  -> sell 109.189601 SLIM into a different Orca/Jupiter destination -> 0.505014 USDC.

Counterfactual:
  passively absorb that exact 109.189601 SLIM at the observed destination sell
  price (cost 0.505014 USDC), then immediately sell the same quantity into A
  using A's exact post-index-735 vault state and Raydium AMM-v4 formula.

This measures economics only. It does not prove a resting maker order can be
placed at the destination venue.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

getcontext().prec = 60

SLOT=443464889
INDEX=735
SIG="5wsGYRDV5GdpipTRW2Bf1FuXngGfPovbzaVdJYJerzet7HSNeT7iau9T1ffe2fsfBcGKVEUTLmWXBWTcjDwTAeJn"
A_POOL="8idN93ZBpdtMp4672aS4GGMDy7LdVWCCXH7FKFdMw9P4"
A_SLIM_VAULT="6FoSD24CM2MyadTwVUqgZQ17kXozfMa3DfusbnuqYduy"
A_SOL_VAULT="EDL73XTnmr56U4ohW5uXXh6LJwsQQdoRLragMYEWLGPn"
SLIM="xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
WSOL="So11111111111111111111111111111111111111112"
USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

OBSERVED_B2_SLIM_RAW=109_189_601
OBSERVED_B2_USDC_RAW=505_014
# The same landed route spent exactly 0.5 USDC to obtain the SOL fed into A.
ROUTE_USDC_IN_RAW=500_000
ROUTE_SOL_TO_A_RAW=4_912_405

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
    fee=ceil_fraction(amt,fn,fd); net=amt-fee; amount_out=out*net//(inp+net)
    return {"fee":fee,"net_in":net,"amount_out":amount_out}
def ui(raw:int,dec:int)->Decimal:return Decimal(raw)/(Decimal(10)**dec)

def main()->None:
    block=rpc("getBlock",[SLOT,{"commitment":"finalized","transactionDetails":"full","encoding":"jsonParsed","rewards":False,"maxSupportedTransactionVersion":0}])
    tx=(block.get("transactions") or [])[INDEX]
    sig=str(((tx.get("transaction") or {}).get("signatures") or [""])[0])
    if sig!=SIG:raise RuntimeError(f"anchor mismatch {sig}")
    static=load_static()
    if static["coin_mint"]!=SLIM or static["pc_mint"]!=WSOL or static["coin_vault"]!=A_SLIM_VAULT or static["pc_vault"]!=A_SOL_VAULT:
        raise RuntimeError(f"unexpected A state {static}")
    slim=token_balance(tx,A_SLIM_VAULT); sol=token_balance(tx,A_SOL_VAULT)
    if not slim or not sol:raise RuntimeError("A vault balances absent")
    slim_pre,slim_post,slim_dec,_=slim; sol_pre,sol_post,sol_dec,_=sol
    if slim_pre-slim_post!=OBSERVED_B2_SLIM_RAW or sol_post-sol_pre!=ROUTE_SOL_TO_A_RAW:
        raise RuntimeError(f"landed A amounts changed: slim={slim_pre-slim_post} sol={sol_post-sol_pre}")

    # A exact post-735 state, adjusted by static NeedTakePnl as in Raydium quote logic.
    slim_res=slim_post-int(static["need_take_pnl_coin"])
    sol_res=sol_post-int(static["need_take_pnl_pc"])
    exitq=quote_out(slim_res,sol_res,OBSERVED_B2_SLIM_RAW,int(static["fee_num"]),int(static["fee_den"]))
    exit_sol=ui(exitq["amount_out"],sol_dec)
    cost=ui(OBSERVED_B2_USDC_RAW,6)
    b2_avg=cost/ui(OBSERVED_B2_SLIM_RAW,slim_dec)

    # Two transparent references:
    # 1) contemporaneous route-implied USDC/SOL from the same tx's USDC->SOL leg;
    # 2) a nearby external Binance reference used in the earlier reconstruction.
    route_solusd=ui(ROUTE_USDC_IN_RAW,6)/ui(ROUTE_SOL_TO_A_RAW,sol_dec)
    refs={"same_tx_route_implied":route_solusd,"nearby_binance_1s":Decimal("101.75")}
    scenarios=[]
    for label,px in refs.items():
        value=exit_sol*px; pnl=value-cost
        scenarios.append({"reference":label,"solusd":str(px),"exit_value_usd":str(value),"pnl_usd":str(pnl),"pnl_bps":str(pnl/cost*Decimal(10000))})

    result={
      "event":{"slot":SLOT,"index":INDEX,"signature":SIG},
      "A":{"pool":A_POOL,"venue":"Raydium AMM v4","slim_post_raw":slim_post,"sol_post_raw":sol_post,
           "landed_buy_slim":str(ui(OBSERVED_B2_SLIM_RAW,slim_dec)),"landed_sol_in":str(ui(ROUTE_SOL_TO_A_RAW,sol_dec)),
           "immediate_reverse_quote":exitq,"immediate_reverse_sol":str(exit_sol)},
      "B2":{"venue_candidate":"Orca Whirlpool/Jupiter route","candidate_state":"Cbuc6RwKvkdUXz2f1DKeSZsknrwvqXdWSjYqxQKaUDwp",
            "landed_slim_in":str(ui(OBSERVED_B2_SLIM_RAW,slim_dec)),"landed_usdc_out":str(cost),"avg_usdc_per_slim":str(b2_avg),
            "maker_executability_proven":False},
      "passive_capture":{"hypothetical_cost_usdc":str(cost),"hypothetical_A_exit_sol":str(exit_sol),
                         "break_even_solusd":str(cost/exit_sol),"scenarios":scenarios,
                         "maker_executability_proven":False}
    }
    p=Path("research/output/passive_capture/slim_b2_reconstruction.json");p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("# SLIM B2 passive-capture @ index 735")
    print("B2 observed",ui(OBSERVED_B2_SLIM_RAW,slim_dec),"SLIM ->",cost,"USDC avg",b2_avg)
    print("Immediate A reverse",ui(OBSERVED_B2_SLIM_RAW,slim_dec),"SLIM ->",exit_sol,"SOL")
    print("Break-even SOLUSD",cost/exit_sol)
    for s in scenarios:print(s)

if __name__=="__main__":main()
