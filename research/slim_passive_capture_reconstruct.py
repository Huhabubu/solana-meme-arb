#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reconstruct the corrected passive-capture strategy on the clean SLIM SELL shock.

Observed landed sequence:
  A = Raydium AMM v4 SLIM/SOL pool 8idN...
  H0 index 160: a large trader sells SLIM into A, depressing A.
  H1 index 161: MRiYA4 converts USDC->SOL, buys SLIM from A, then sells SLIM
                into B (Orca legacy token-swap, SLIM/USDC).

Counterfactual studied here:
  Assume we can passively absorb the quantity MRiYA4 sells into B at the
  observed B-leg aggregate price. Immediately after that fill, sell the same
  SLIM quantity back into the now-recovering A using A's exact AMM-v4 quote.

IMPORTANT: this is an ECONOMIC counterfactual, not yet a maker-fill proof.
B is an AMM swap venue in this sample; a separate execution layer must establish
how/if a resting order or LP/range position can actually intercept that flow.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, List, Tuple

getcontext().prec = 60

TRIGGER = "2Z5fgLBVCyaHjGuNsHamWdVKSaJMniGne8GiswwgHg87cWT4CznwEcjiVCyvTBMzZULddFnaZapJ9MkuQHHhRU3B"
BOT = "5oK4st7Rc8TzmeGYWS8Qrn9MfpjNhZ9v44KoLwanVeWs2YYESbaDtvKrLJSzKtyVtN6vPzcfLZXw7RYQzqg2fBtz"
A_POOL = "8idN93ZBpdtMp4672aS4GGMDy7LdVWCCXH7FKFdMw9P4"
A_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
B_POOL = "8JPid6GtND2tU3A7x7GDfPPEWwS36rMtzF7YoHU44UoA"
B_AUTHORITY = "749y4fXb9SzqmrLEetQdui5iDucnNiMgCJ2uzc3y7cou"
SLIM = "xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Raydium AMM v4 AmmInfo offsets.
COIN_DECIMALS_OFFSET=32
PC_DECIMALS_OFFSET=40
SWAP_FEE_NUMERATOR_OFFSET=176
SWAP_FEE_DENOMINATOR_OFFSET=184
NEED_TAKE_PNL_COIN_OFFSET=192
NEED_TAKE_PNL_PC_OFFSET=200
COIN_VAULT_OFFSET=336
PC_VAULT_OFFSET=368
COIN_MINT_OFFSET=400
PC_MINT_OFFSET=432
B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n=int.from_bytes(raw,"big"); out=""
    while n:
        n,r=divmod(n,58); out=B58[r]+out
    z=0
    for b in raw:
        if b==0:z+=1
        else:break
    return "1"*z+(out or "")


def rpc_url()->str:
    v=os.getenv("HELIUS_RPC_URL","").strip()
    if v:return v
    k=os.getenv("HELIUS_API_KEY","").strip()
    if k:return f"https://mainnet.helius-rpc.com/?api-key={k}"
    raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")


def rpc(method:str,params:List[Any])->Any:
    payload=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=urllib.request.Request(rpc_url(),data=payload,headers={"content-type":"application/json"})
    with urllib.request.urlopen(req,timeout=90) as resp: body=json.loads(resp.read().decode())
    if body.get("error"):raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def get_tx(sig:str)->Dict[str,Any]:
    return rpc("getTransaction",[sig,{"commitment":"finalized","encoding":"jsonParsed","maxSupportedTransactionVersion":0}])


def u64(raw:bytes,off:int)->int:return int.from_bytes(raw[off:off+8],"little")
def pk(raw:bytes,off:int)->str:return b58encode(raw[off:off+32])


def load_a_state()->Dict[str,Any]:
    v=rpc("getAccountInfo",[A_POOL,{"encoding":"base64","commitment":"finalized"}])["value"]
    raw=base64.b64decode(v["data"][0])
    if len(raw)!=752:raise RuntimeError(f"unexpected A pool state len {len(raw)}")
    return {
        "coin_decimals":u64(raw,COIN_DECIMALS_OFFSET),"pc_decimals":u64(raw,PC_DECIMALS_OFFSET),
        "fee_num":u64(raw,SWAP_FEE_NUMERATOR_OFFSET),"fee_den":u64(raw,SWAP_FEE_DENOMINATOR_OFFSET),
        "need_take_pnl_coin":u64(raw,NEED_TAKE_PNL_COIN_OFFSET),"need_take_pnl_pc":u64(raw,NEED_TAKE_PNL_PC_OFFSET),
        "coin_vault":pk(raw,COIN_VAULT_OFFSET),"pc_vault":pk(raw,PC_VAULT_OFFSET),
        "coin_mint":pk(raw,COIN_MINT_OFFSET),"pc_mint":pk(raw,PC_MINT_OFFSET),
    }


def account_keys(tx:Dict[str,Any])->List[str]:
    out=[]
    for x in tx["transaction"]["message"].get("accountKeys") or []:
        out.append(str(x if isinstance(x,str) else x.get("pubkey") or ""))
    return out


def owner_mint_balances(tx:Dict[str,Any],owner:str,mint:str)->Tuple[int,int,int]:
    keys=account_keys(tx); meta=tx.get("meta") or {}
    pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []}
    post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []}
    pre_sum=post_sum=0; decimals=None
    for i in set(pre)|set(post):
        a,b=pre.get(i,{}),post.get(i,{})
        ref=b or a
        if str(ref.get("owner") or "")!=owner or str(ref.get("mint") or "")!=mint:continue
        ui=ref.get("uiTokenAmount") or {}; decimals=int(ui.get("decimals") or 0)
        def raw(v:Dict[str,Any])->int:return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
        pre_sum+=raw(a); post_sum+=raw(b)
    if decimals is None:raise RuntimeError(f"missing owner/mint balance {owner} {mint}")
    return pre_sum,post_sum,decimals


def ceil_fraction(v:int,n:int,d:int)->int:return (v*n+d-1)//d

def quote_out(inp_res:int,out_res:int,amount_in:int,fee_num:int,fee_den:int)->Dict[str,int]:
    fee=ceil_fraction(amount_in,fee_num,fee_den); net=amount_in-fee
    out=out_res*net//(inp_res+net)
    return {"fee":fee,"net_in":net,"amount_out":out}


def ui(raw:int,dec:int)->Decimal:return Decimal(raw)/(Decimal(10)**dec)

def bps(new:Decimal,old:Decimal)->Decimal:return (new/old-Decimal(1))*Decimal(10000)


def reserve_price(quote_raw:int,base_raw:int,quote_dec:int,base_dec:int)->Decimal:
    return ui(quote_raw,quote_dec)/ui(base_raw,base_dec)


def main()->None:
    trigger=get_tx(TRIGGER); bot=get_tx(BOT); state=load_a_state()
    if {state["coin_mint"],state["pc_mint"]}!={SLIM,WSOL}:
        raise RuntimeError(f"unexpected A pair {state['coin_mint']} {state['pc_mint']}")

    # Historical reserve snapshots by owner/mint.
    t_slim_pre,t_slim_post,slim_dec=owner_mint_balances(trigger,A_AUTHORITY,SLIM)
    t_sol_pre,t_sol_post,sol_dec=owner_mint_balances(trigger,A_AUTHORITY,WSOL)
    a_slim_pre,a_slim_post,_=owner_mint_balances(bot,A_AUTHORITY,SLIM)
    a_sol_pre,a_sol_post,_=owner_mint_balances(bot,A_AUTHORITY,WSOL)
    b_slim_pre,b_slim_post,_=owner_mint_balances(bot,B_AUTHORITY,SLIM)
    b_usdc_pre,b_usdc_post,usdc_dec=owner_mint_balances(bot,B_AUTHORITY,USDC)

    # Continuity: bot's A pre-state must equal trigger's A post-state because txs are adjacent.
    continuity={"slim_raw":a_slim_pre-t_slim_post,"sol_raw":a_sol_pre-t_sol_post}

    # Map static PnL deductions to mints.
    if state["coin_mint"]==SLIM:
        slim_pnl=state["need_take_pnl_coin"]; sol_pnl=state["need_take_pnl_pc"]
        slim_pool_dec=state["coin_decimals"]; sol_pool_dec=state["pc_decimals"]
    else:
        slim_pnl=state["need_take_pnl_pc"]; sol_pnl=state["need_take_pnl_coin"]
        slim_pool_dec=state["pc_decimals"]; sol_pool_dec=state["coin_decimals"]
    if slim_pool_dec!=slim_dec or sol_pool_dec!=sol_dec:
        raise RuntimeError("A state decimals disagree with tx metadata")

    def a_price(slim_raw:int,sol_raw:int)->Decimal:
        return reserve_price(sol_raw-sol_pnl,slim_raw-slim_pnl,sol_dec,slim_dec)

    a_pre_trigger=a_price(t_slim_pre,t_sol_pre)
    a_post_trigger=a_price(t_slim_post,t_sol_post)
    a_post_bot=a_price(a_slim_post,a_sol_post)
    a_shock=bps(a_post_trigger,a_pre_trigger)
    a_recovery_from_bottom=bps(a_post_bot,a_post_trigger)
    a_residual=bps(a_post_bot,a_pre_trigger)

    # Verify the landed bot A buy against the exact AMM-v4 formula.
    observed_sol_in=a_sol_post-a_sol_pre
    observed_slim_out=a_slim_pre-a_slim_post
    a_quote_verify=quote_out(a_sol_pre-sol_pnl,a_slim_pre-slim_pnl,observed_sol_in,state["fee_num"],state["fee_den"])

    # B sell pressure from actual historical reserves.
    b_pre=reserve_price(b_usdc_pre,b_slim_pre,usdc_dec,slim_dec)
    b_post=reserve_price(b_usdc_post,b_slim_post,usdc_dec,slim_dec)
    b_pressure=bps(b_post,b_pre)
    b_slim_in=b_slim_post-b_slim_pre
    b_usdc_out=b_usdc_pre-b_usdc_post
    b_avg_fill=ui(b_usdc_out,usdc_dec)/ui(b_slim_in,slim_dec)

    # Corrected passive-capture counterfactual:
    # buy the exact B-leg SLIM quantity for the exact USDC B paid out, then immediately
    # sell that SLIM into A after the arb bot has already lifted A.
    passive_qty=b_slim_in
    a_exit=quote_out(a_slim_post-slim_pnl,a_sol_post-sol_pnl,passive_qty,state["fee_num"],state["fee_den"])
    a_exit_sol=ui(a_exit["amount_out"],sol_dec)
    passive_cost_usdc=ui(b_usdc_out,usdc_dec)
    break_even_solusdc=passive_cost_usdc/a_exit_sol

    # Use contemporaneous Binance SOLUSDT externally; these are supplied only as a
    # small sensitivity table so the result is transparent and not API-dependent.
    refs=[Decimal("101.74"),Decimal("101.75"),Decimal("101.76"),Decimal("101.77")]
    sensitivity=[]
    for px in refs:
        value=a_exit_sol*px
        profit=value-passive_cost_usdc
        sensitivity.append({"solusdt":str(px),"a_exit_value_usd":str(value),"profit_usd":str(profit),"profit_bps":str(profit/passive_cost_usdc*Decimal(10000))})

    result={
        "event":{"slot":int(trigger["slot"]),"trigger_index":160,"bot_index":161,"trigger":TRIGGER,"bot":BOT},
        "strategy_definition":"SELL shock A -> arb buys A / sells B -> hypothetical passive buy B -> immediate taker sell A",
        "A":{"venue":"Raydium AMM v4","pool":A_POOL,"pair":"SLIM/WSOL","state":state,
             "continuity_trigger_post_to_bot_pre":continuity,
             "pre_trigger_sol_per_slim":str(a_pre_trigger),"post_trigger_sol_per_slim":str(a_post_trigger),
             "post_bot_sol_per_slim":str(a_post_bot),"trigger_shock_bps":str(a_shock),
             "recovery_from_post_trigger_bps":str(a_recovery_from_bottom),"residual_vs_pre_trigger_bps":str(a_residual),
             "bot_buy":{"sol_in":str(ui(observed_sol_in,sol_dec)),"slim_out":str(ui(observed_slim_out,slim_dec)),
                        "quote_reproduction_raw":a_quote_verify,"exact_match":a_quote_verify["amount_out"]==observed_slim_out}},
        "B":{"venue":"Orca legacy token-swap","program":"9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP","pool":B_POOL,"pair":"SLIM/USDC",
             "pre_usdc_per_slim":str(b_pre),"post_usdc_per_slim":str(b_post),"arb_sell_pressure_bps":str(b_pressure),
             "arb_slim_in":str(ui(b_slim_in,slim_dec)),"arb_usdc_out":str(ui(b_usdc_out,usdc_dec)),"arb_avg_usdc_per_slim":str(b_avg_fill)},
        "passive_capture_counterfactual":{"maker_executability_proven":False,
             "note":"B is an AMM swap venue; observed arb flow proves economic direction, not that a native resting limit order would be filled.",
             "hypothetical_b_fill_slim":str(ui(passive_qty,slim_dec)),"hypothetical_b_cost_usdc":str(passive_cost_usdc),
             "immediate_A_exit_sol":str(a_exit_sol),"A_exit_quote_raw":a_exit,
             "break_even_SOLUSDC":str(break_even_solusdc),"binance_SOLUSDT_sensitivity":sensitivity},
    }
    out=Path("research/output/passive_capture/slim_reconstruction.json")
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")

    print("# SLIM corrected passive-capture reconstruction")
    print("A shock bps",a_shock,"A recovery bps",a_recovery_from_bottom,"A residual",a_residual)
    print("A quote reproduction exact",a_quote_verify["amount_out"]==observed_slim_out,a_quote_verify,"observed",observed_slim_out)
    print("B pressure bps",b_pressure,"B avg fill",b_avg_fill,"USDC/SLIM")
    print("hypothetical passive B cost",passive_cost_usdc,"USDC for",ui(passive_qty,slim_dec),"SLIM")
    print("immediate A exit",a_exit_sol,"SOL; break-even SOLUSDC",break_even_solusdc)
    for r in sensitivity:print("SOLUSDT",r["solusdt"],"profit",r["profit_usd"],"USD",r["profit_bps"],"bps")

if __name__=="__main__":main()
