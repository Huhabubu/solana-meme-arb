#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure the second-order price impact on the RAY/USDC Raydium AMM v4 pool.

The clean event is:
  large trade -> Raydium CLMM RAY/SOL
  immediately next tx MRiYA4 buys RAY on Raydium AMM v4, sells it on that CLMM,
  then converts SOL back to USDC.

This script checks whether the current static AMM v4 state fields reproduce the
landed historical swap exactly using historical pre-vault balances from the
transaction. If yes, it uses effective reserves (vault - need_take_pnl) to
measure the AMM pool's pre/post marginal reserve-ratio price movement caused by
the arbitrage bot itself.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, List

getcontext().prec = 50

BOT_TX = "5HffqQCAvsiTqMi4LwB8HJBhi96BNc1QnvNj1vVrXLZipTHX7NxeZp5EggUK3HSff4rxAf6cj92QqM4tutASRPvw"
POOL = "6UmmUiYoBjSrhakAobJw8BvkmJtDVxaeBtbt7rxWo1mg"
RAY = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Raydium AMM v4 packed AmmInfo offsets, matching src/dex/raydium_amm.rs.
COIN_DECIMALS_OFFSET = 32
PC_DECIMALS_OFFSET = 40
SWAP_FEE_NUMERATOR_OFFSET = 176
SWAP_FEE_DENOMINATOR_OFFSET = 184
NEED_TAKE_PNL_COIN_OFFSET = 192
NEED_TAKE_PNL_PC_OFFSET = 200
COIN_VAULT_OFFSET = 336
PC_VAULT_OFFSET = 368
COIN_MINT_OFFSET = 400
PC_MINT_OFFSET = 432

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    z = 0
    for b in raw:
        if b == 0:
            z += 1
        else:
            break
    return "1" * z + (out or "")


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
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def u64(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off+8], "little")


def pubkey(data: bytes, off: int) -> str:
    return b58encode(data[off:off+32])


def load_state() -> Dict[str, Any]:
    v = rpc("getAccountInfo", [POOL, {"encoding":"base64","commitment":"finalized"}])["value"]
    raw = base64.b64decode(v["data"][0])
    if len(raw) != 752:
        raise RuntimeError(f"unexpected AMM account length {len(raw)}")
    return {
        "coin_decimals": u64(raw, COIN_DECIMALS_OFFSET),
        "pc_decimals": u64(raw, PC_DECIMALS_OFFSET),
        "fee_num": u64(raw, SWAP_FEE_NUMERATOR_OFFSET),
        "fee_den": u64(raw, SWAP_FEE_DENOMINATOR_OFFSET),
        "need_take_pnl_coin": u64(raw, NEED_TAKE_PNL_COIN_OFFSET),
        "need_take_pnl_pc": u64(raw, NEED_TAKE_PNL_PC_OFFSET),
        "coin_vault": pubkey(raw, COIN_VAULT_OFFSET),
        "pc_vault": pubkey(raw, PC_VAULT_OFFSET),
        "coin_mint": pubkey(raw, COIN_MINT_OFFSET),
        "pc_mint": pubkey(raw, PC_MINT_OFFSET),
    }


def tx_vault_balances() -> Dict[str, Dict[str, int]]:
    tx = rpc("getTransaction", [BOT_TX, {
        "commitment":"finalized","encoding":"jsonParsed","maxSupportedTransactionVersion":0
    }])
    keys = []
    for x in tx["transaction"]["message"]["accountKeys"]:
        keys.append(str(x if isinstance(x,str) else x.get("pubkey") or ""))
    pre = {int(x["accountIndex"]):x for x in tx["meta"].get("preTokenBalances") or []}
    post = {int(x["accountIndex"]):x for x in tx["meta"].get("postTokenBalances") or []}
    out: Dict[str, Dict[str,int]] = {}
    for i in set(pre) | set(post):
        account = keys[i]
        a, b = pre.get(i,{}), post.get(i,{})
        ref = b or a
        mint = str(ref.get("mint") or "")
        def raw(x: Dict[str,Any]) -> int:
            return int(((x.get("uiTokenAmount") or {}).get("amount") or 0))
        out[account] = {"mint":mint,"pre":raw(a),"post":raw(b)}
    return out


def ceil_fraction(v: int, n: int, d: int) -> int:
    return (v*n + d - 1)//d


def quote_out(input_reserve: int, output_reserve: int, amount_in: int, fee_num: int, fee_den: int) -> Dict[str,int]:
    fee = ceil_fraction(amount_in, fee_num, fee_den)
    net = amount_in - fee
    out = output_reserve * net // (input_reserve + net)
    return {"fee":fee,"net_in":net,"amount_out":out}


def ui_ratio(pc_raw: int, coin_raw: int, coin_dec: int, pc_dec: int) -> Decimal:
    # PC per coin in UI units.
    pc_ui = Decimal(pc_raw)/(Decimal(10)**pc_dec)
    coin_ui = Decimal(coin_raw)/(Decimal(10)**coin_dec)
    return pc_ui/coin_ui


def main() -> None:
    state = load_state()
    bals = tx_vault_balances()
    if state["coin_mint"] != RAY or state["pc_mint"] != USDC:
        raise RuntimeError(f"unexpected pool pair {state['coin_mint']} / {state['pc_mint']}")
    cv, pv = state["coin_vault"], state["pc_vault"]
    coin = bals[cv]
    pc = bals[pv]

    pre_coin_eff = coin["pre"] - state["need_take_pnl_coin"]
    pre_pc_eff = pc["pre"] - state["need_take_pnl_pc"]
    post_coin_eff = coin["post"] - state["need_take_pnl_coin"]
    post_pc_eff = pc["post"] - state["need_take_pnl_pc"]

    observed_input = pc["post"] - pc["pre"]
    observed_output = coin["pre"] - coin["post"]
    quoted = quote_out(
        pre_pc_eff, pre_coin_eff, observed_input,
        state["fee_num"], state["fee_den"]
    )

    pre_price = ui_ratio(pre_pc_eff, pre_coin_eff, state["coin_decimals"], state["pc_decimals"])
    post_price = ui_ratio(post_pc_eff, post_coin_eff, state["coin_decimals"], state["pc_decimals"])
    move_bps = (post_price/pre_price - Decimal(1))*Decimal(10000)
    avg_exec = (Decimal(observed_input)/(Decimal(10)**state["pc_decimals"])) / (Decimal(observed_output)/(Decimal(10)**state["coin_decimals"]))

    result = {
        "pool": POOL,
        "pair": "RAY/USDC",
        "coin_vault": cv,
        "pc_vault": pv,
        "state": state,
        "historical_vaults": {
            "ray_pre_raw": coin["pre"], "ray_post_raw": coin["post"],
            "usdc_pre_raw": pc["pre"], "usdc_post_raw": pc["post"],
        },
        "observed_swap": {
            "usdc_in_raw": observed_input,
            "ray_out_raw": observed_output,
            "avg_execution_usdc_per_ray": str(avg_exec),
        },
        "quote_reproduction": {
            **quoted,
            "observed_output_raw": observed_output,
            "output_error_raw": quoted["amount_out"]-observed_output,
            "exact_match": quoted["amount_out"] == observed_output,
        },
        "marginal_reserve_ratio_price": {
            "pre_usdc_per_ray": str(pre_price),
            "post_usdc_per_ray": str(post_price),
            "bot_induced_move_bps": str(move_bps),
        },
    }
    out = Path("research/output/ray_clean_event_reconstruct/amm_second_order.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")

    print("RAY/USDC AMM v4 pool", POOL)
    print("state", state)
    print("observed USDC in", Decimal(observed_input)/Decimal(1_000_000))
    print("observed RAY out", Decimal(observed_output)/Decimal(1_000_000))
    print("quote reproduction", quoted, "observed", observed_output, "exact", quoted["amount_out"]==observed_output)
    print("pre marginal", pre_price, "USDC/RAY")
    print("avg execution", avg_exec, "USDC/RAY")
    print("post marginal", post_price, "USDC/RAY")
    print("second-order move", move_bps, "bps")


if __name__ == "__main__":
    main()
