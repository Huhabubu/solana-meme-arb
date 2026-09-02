#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reconstruct the clean RAY trigger -> MRiYA4 arbitrage event.

This script uses only landed Solana transaction data:
- ledger order from getBlock;
- Raydium CLMM SwapEvent (sqrtPriceX64/liquidity/tick/fees);
- parsed SPL token transfers for each arbitrage leg;
- explicit Jito tip transfer and transaction fee.

For this clean sample the trigger and bot transaction are adjacent in the same slot,
so the CLMM post-trigger state is also the bot CLMM pre-state.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import urllib.request
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

getcontext().prec = 60

TRIGGER_DEFAULT = "27oKQ1VHcA5HyECpispRQqKS6wtvzsUXjY28B62sQ1833BccxJoMBvGZqqgQvwogBF353w8boENB3VTsJqBAZim2"
BOT_TX_DEFAULT = "5HffqQCAvsiTqMi4LwB8HJBhi96BNc1QnvNj1vVrXLZipTHX7NxeZp5EggUK3HSff4rxAf6cj92QqM4tutASRPvw"
RAY_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BOT = "MRiYA4oN3158fCV8evhuCofrDzbHyYvYnGZUDJvoCsa"

RAYDIUM_CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
TESSERA = "TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH"
BOT_ROUTER = "AN225ksocfZpbPVEbwffiANbtUdYbySM3WJ6dbsYAqij"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

PROGRAM_LABELS = {
    RAYDIUM_CLMM: "Raydium CLMM",
    RAYDIUM_AMM_V4: "Raydium AMM v4",
    TESSERA: "Tessera",
    BOT_ROUTER: "MRiYA4 execution/router",
}

JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
Q64 = Decimal(2) ** 64
SWAP_EVENT_DISC = hashlib.sha256(b"event:SwapEvent").digest()[:8]


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    s = ""
    while n:
        n, rem = divmod(n, 58)
        s = B58_ALPHABET[rem] + s
    zeros = 0
    for b in raw:
        if b == 0:
            zeros += 1
        else:
            break
    return "1" * zeros + (s or "")


def rpc_url() -> str:
    url = os.getenv("HELIUS_RPC_URL", "").strip()
    if url:
        return url
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")


def rpc(method: str, params: List[Any]) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url(), data=payload, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def get_tx(sig: str) -> Dict[str, Any]:
    tx = rpc("getTransaction", [sig, {
        "commitment": "finalized",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
    }])
    if tx is None:
        raise RuntimeError(f"transaction not found: {sig}")
    return tx


def account_keys(tx: Dict[str, Any]) -> List[str]:
    rows = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    out = []
    for x in rows:
        out.append(str(x if isinstance(x, str) else x.get("pubkey") or ""))
    return out


def ix_program(ix: Dict[str, Any], keys: List[str]) -> str:
    if ix.get("programId"):
        return str(ix["programId"])
    i = ix.get("programIdIndex")
    if isinstance(i, int) and 0 <= i < len(keys):
        return keys[i]
    return ""


def ix_accounts(ix: Dict[str, Any], keys: List[str]) -> List[str]:
    out = []
    for x in ix.get("accounts") or []:
        if isinstance(x, int) and 0 <= x < len(keys):
            out.append(keys[x])
        else:
            out.append(str(x))
    return out


def inner_instructions(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    keys = account_keys(tx)
    rows = []
    for grp in (tx.get("meta") or {}).get("innerInstructions") or []:
        outer = int(grp.get("index", -1))
        for j, ix in enumerate(grp.get("instructions") or []):
            if not isinstance(ix, dict):
                continue
            rows.append({
                "outer": outer,
                "inner": j,
                "program_id": ix_program(ix, keys),
                "accounts": ix_accounts(ix, keys),
                "parsed": ix.get("parsed"),
            })
    return rows


def parsed_transfer(ix: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ix.get("program_id") != TOKEN_PROGRAM:
        return None
    p = ix.get("parsed")
    if not isinstance(p, dict) or p.get("type") not in {"transfer", "transferChecked"}:
        return None
    info = p.get("info") or {}
    if "tokenAmount" in info:
        ta = info["tokenAmount"] or {}
        amount_raw = int(ta.get("amount") or 0)
        decimals = int(ta.get("decimals") or 0)
        amount_ui = Decimal(amount_raw) / (Decimal(10) ** decimals)
        mint = str(info.get("mint") or "")
    else:
        amount_raw = int(info.get("amount") or 0)
        amount_ui = None
        mint = ""
    return {
        "source": str(info.get("source") or ""),
        "destination": str(info.get("destination") or ""),
        "authority": str(info.get("authority") or ""),
        "mint": mint,
        "amount_raw": amount_raw,
        "amount_ui": amount_ui,
    }


def token_account_meta(tx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    keys = account_keys(tx)
    meta = tx.get("meta") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for side in ("preTokenBalances", "postTokenBalances"):
        for row in meta.get(side) or []:
            idx = int(row["accountIndex"])
            if idx >= len(keys):
                continue
            ui = row.get("uiTokenAmount") or {}
            out[keys[idx]] = {
                "mint": str(row.get("mint") or ""),
                "owner": str(row.get("owner") or ""),
                "decimals": int(ui.get("decimals") or 0),
            }
    return out


def decode_clmm_swap_events(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = []
    for log in (tx.get("meta") or {}).get("logMessages") or []:
        if not log.startswith("Program data: "):
            continue
        raw = base64.b64decode(log.split(": ", 1)[1])
        if len(raw) < 205 or raw[:8] != SWAP_EVENT_DISC:
            continue
        off = 8
        pubs = []
        for _ in range(4):
            pubs.append(b58encode(raw[off:off+32])); off += 32
        vals = []
        for _ in range(4):
            vals.append(int.from_bytes(raw[off:off+8], "little")); off += 8
        zero_for_one = bool(raw[off]); off += 1
        sqrt_price_x64 = int.from_bytes(raw[off:off+16], "little"); off += 16
        liquidity = int.from_bytes(raw[off:off+16], "little"); off += 16
        tick = int.from_bytes(raw[off:off+4], "little", signed=True); off += 4
        trade_fee_0 = int.from_bytes(raw[off:off+8], "little") if len(raw) >= off+8 else 0; off += 8
        trade_fee_1 = int.from_bytes(raw[off:off+8], "little") if len(raw) >= off+8 else 0
        events.append({
            "pool_state": pubs[0],
            "sender": pubs[1],
            "token_account_0": pubs[2],
            "token_account_1": pubs[3],
            "amount_0_raw": vals[0],
            "transfer_fee_0_raw": vals[1],
            "amount_1_raw": vals[2],
            "transfer_fee_1_raw": vals[3],
            "zero_for_one": zero_for_one,
            "sqrt_price_x64": sqrt_price_x64,
            "liquidity": liquidity,
            "tick": tick,
            "trade_fee_0_raw": trade_fee_0,
            "trade_fee_1_raw": trade_fee_1,
        })
    return events


def event_mints_and_decimals(tx: Dict[str, Any], ev: Dict[str, Any]) -> Tuple[str, int, str, int]:
    tm = token_account_meta(tx)
    a = tm.get(ev["token_account_0"], {})
    b = tm.get(ev["token_account_1"], {})
    return str(a.get("mint") or ""), int(a.get("decimals") or 0), str(b.get("mint") or ""), int(b.get("decimals") or 0)


def ui_price_token1_per_token0(sqrt_price_x64: Decimal, decimals0: int, decimals1: int) -> Decimal:
    raw = (sqrt_price_x64 / Q64) ** 2
    return raw * (Decimal(10) ** (decimals0 - decimals1))


def infer_trigger_pre_sqrt(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Infer pre-swap sqrt price for this clean event and return a consistency check.

    The sample is zero_for_one (SOL -> RAY). If liquidity is unchanged across the
    active range, amount1 = L * (sqrt_before - sqrt_after) / Q64. We then check
    the independent amount0 equation after subtracting the emitted trade fee.
    """
    if not ev["zero_for_one"]:
        raise RuntimeError("clean RAY trigger is expected to be zero_for_one")
    s1 = Decimal(ev["sqrt_price_x64"])
    liq = Decimal(ev["liquidity"])
    amount1 = Decimal(ev["amount_1_raw"] - ev["transfer_fee_1_raw"])
    s0 = s1 + amount1 * Q64 / liq
    net_amount0 = Decimal(ev["amount_0_raw"] - ev["trade_fee_0_raw"] - ev["transfer_fee_0_raw"])
    predicted_amount0 = liq * (s0 - s1) * Q64 / (s0 * s1)
    error_raw = predicted_amount0 - net_amount0
    return {
        "pre_sqrt_price_x64": s0,
        "amount0_net_actual_raw": net_amount0,
        "amount0_net_predicted_raw": predicted_amount0,
        "amount0_consistency_error_raw": error_raw,
        "amount0_consistency_error_bps": (error_raw / net_amount0) * Decimal(10000),
    }


def infer_one_for_zero_pre_sqrt(ev: Dict[str, Any]) -> Decimal:
    if ev["zero_for_one"]:
        raise RuntimeError("expected one_for_zero")
    s1 = Decimal(ev["sqrt_price_x64"])
    liq = Decimal(ev["liquidity"])
    net_amount1 = Decimal(ev["amount_1_raw"] - ev["trade_fee_1_raw"] - ev["transfer_fee_1_raw"])
    return s1 - net_amount1 * Q64 / liq


def ledger_positions(slot: int, trigger_sig: str, bot_sig: str) -> Tuple[int, int]:
    block = rpc("getBlock", [slot, {
        "commitment": "finalized",
        "transactionDetails": "signatures",
        "rewards": False,
        "maxSupportedTransactionVersion": 0,
    }])
    sigs = block.get("signatures") or []
    return sigs.index(trigger_sig), sigs.index(bot_sig)


def group_bot_venue_legs(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [r for r in inner_instructions(tx) if r["outer"] == 2]
    legs: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for r in rows:
        pid = r["program_id"]
        if pid in {RAYDIUM_AMM_V4, RAYDIUM_CLMM, TESSERA}:
            if current:
                legs.append(current)
            current = {
                "program_id": pid,
                "venue": PROGRAM_LABELS[pid],
                "accounts": r["accounts"],
                "transfers": [],
            }
            continue
        t = parsed_transfer(r)
        if current and t:
            current["transfers"].append(t)
    if current:
        legs.append(current)

    token_meta = token_account_meta(tx)
    for leg in legs:
        for t in leg["transfers"]:
            if not t["mint"]:
                m = token_meta.get(t["source"]) or token_meta.get(t["destination"]) or {}
                t["mint"] = str(m.get("mint") or "")
                dec = int(m.get("decimals") or 0)
                t["amount_ui"] = Decimal(t["amount_raw"]) / (Decimal(10) ** dec)
        if leg["program_id"] == RAYDIUM_AMM_V4:
            leg["pool_state"] = leg["accounts"][1] if len(leg["accounts"]) > 1 else ""
        elif leg["program_id"] == TESSERA:
            leg["pool_state"] = leg["accounts"][1] if len(leg["accounts"]) > 1 else ""
        else:
            leg["pool_state"] = "from_clmm_event"
    return legs


def find_jito_tip(tx: Dict[str, Any]) -> Dict[str, Any]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = account_keys(tx)
    found = []
    for ix in msg.get("instructions") or []:
        if not isinstance(ix, dict) or ix_program(ix, keys) != SYSTEM_PROGRAM:
            continue
        p = ix.get("parsed")
        if not isinstance(p, dict) or p.get("type") != "transfer":
            continue
        info = p.get("info") or {}
        dest = str(info.get("destination") or "")
        if dest in JITO_TIP_ACCOUNTS:
            found.append({"destination": dest, "lamports": int(info.get("lamports") or 0)})
    lamports = sum(x["lamports"] for x in found)
    return {"transfers": found, "lamports": lamports, "sol": Decimal(lamports) / Decimal(1_000_000_000)}


def D(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def jsonable(x: Any) -> Any:
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, list):
        return [jsonable(v) for v in x]
    return x


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--trigger", default=TRIGGER_DEFAULT)
    p.add_argument("--bot-tx", default=BOT_TX_DEFAULT)
    p.add_argument("--output-dir", default="research/output/ray_clean_event_reconstruct")
    args = p.parse_args()

    trigger_tx = get_tx(args.trigger)
    bot_tx = get_tx(args.bot_tx)
    if trigger_tx["slot"] != bot_tx["slot"]:
        raise RuntimeError("clean sample unexpectedly spans different slots")
    slot = int(trigger_tx["slot"])
    trigger_idx, bot_idx = ledger_positions(slot, args.trigger, args.bot_tx)

    trigger_events = decode_clmm_swap_events(trigger_tx)
    bot_events = decode_clmm_swap_events(bot_tx)
    if len(trigger_events) != 1 or len(bot_events) != 1:
        raise RuntimeError(f"expected one CLMM event in each tx, got {len(trigger_events)} / {len(bot_events)}")
    tev, bev = trigger_events[0], bot_events[0]
    if tev["pool_state"] != bev["pool_state"]:
        raise RuntimeError("trigger and bot did not touch the same CLMM pool")

    mint0, dec0, mint1, dec1 = event_mints_and_decimals(trigger_tx, tev)
    inferred = infer_trigger_pre_sqrt(tev)
    bot_pre_sqrt = infer_one_for_zero_pre_sqrt(bev)

    p_pre = ui_price_token1_per_token0(D(inferred["pre_sqrt_price_x64"]), dec0, dec1)
    p_post_trigger = ui_price_token1_per_token0(D(tev["sqrt_price_x64"]), dec0, dec1)
    p_post_bot = ui_price_token1_per_token0(D(bev["sqrt_price_x64"]), dec0, dec1)

    # Here token0=WSOL and token1=RAY. RAY price in SOL is the inverse.
    ray_sol_pre = Decimal(1) / p_pre
    ray_sol_post_trigger = Decimal(1) / p_post_trigger
    ray_sol_post_bot = Decimal(1) / p_post_bot

    legs = group_bot_venue_legs(bot_tx)
    if len(legs) != 3:
        raise RuntimeError(f"expected 3 venue legs, got {len(legs)}")

    # Normalize known three-leg route from transfer pairs.
    leg_summaries = []
    for leg in legs:
        if len(leg["transfers"]) < 2:
            raise RuntimeError(f"venue leg missing transfers: {leg['venue']}")
        t_in, t_out = leg["transfers"][0], leg["transfers"][1]
        amount_in = D(t_in["amount_ui"])
        amount_out = D(t_out["amount_ui"])
        leg_summaries.append({
            "venue": leg["venue"],
            "program_id": leg["program_id"],
            "pool_state": tev["pool_state"] if leg["program_id"] == RAYDIUM_CLMM else leg["pool_state"],
            "mint_in": t_in["mint"],
            "amount_in": amount_in,
            "mint_out": t_out["mint"],
            "amount_out": amount_out,
            "execution_out_per_in": amount_out / amount_in,
        })

    # Route is USDC -> RAY -> SOL -> USDC.
    initial_usdc = leg_summaries[0]["amount_in"]
    final_usdc = leg_summaries[-1]["amount_out"]
    gross_profit = final_usdc - initial_usdc
    gross_bps = gross_profit / initial_usdc * Decimal(10000)
    implied_clmm_usdc_per_ray = final_usdc / leg_summaries[1]["amount_in"]
    amm_usdc_per_ray = initial_usdc / leg_summaries[0]["amount_out"]

    tip = find_jito_tip(bot_tx)
    sol_usdc_exec = final_usdc / leg_summaries[-1]["amount_in"]
    base_fee_lamports = int((bot_tx.get("meta") or {}).get("fee") or 0)
    base_fee_sol = Decimal(base_fee_lamports) / Decimal(1_000_000_000)
    tip_usdc = tip["sol"] * sol_usdc_exec
    base_fee_usdc = base_fee_sol * sol_usdc_exec
    net_profit_after_tip_fee = gross_profit - tip_usdc - base_fee_usdc
    net_bps_after_tip_fee = net_profit_after_tip_fee / initial_usdc * Decimal(10000)

    report = {
        "event": {
            "slot": slot,
            "trigger_index": trigger_idx,
            "bot_index": bot_idx,
            "index_delta": bot_idx - trigger_idx,
            "trigger_tx": args.trigger,
            "bot_tx": args.bot_tx,
            "bot": BOT,
        },
        "trigger_clmm": {
            "venue": "Raydium CLMM",
            "program_id": RAYDIUM_CLMM,
            "pool_state": tev["pool_state"],
            "token0_mint": mint0,
            "token1_mint": mint1,
            "amount0_raw": tev["amount_0_raw"],
            "amount1_raw": tev["amount_1_raw"],
            "trade_fee0_raw": tev["trade_fee_0_raw"],
            "trade_fee1_raw": tev["trade_fee_1_raw"],
            "post_sqrt_price_x64": tev["sqrt_price_x64"],
            "post_liquidity": tev["liquidity"],
            "post_tick": tev["tick"],
            "inferred_pre_sqrt_price_x64": inferred["pre_sqrt_price_x64"],
            "inference_consistency_error_bps": inferred["amount0_consistency_error_bps"],
            "pre_ray_per_sol": p_pre,
            "post_trigger_ray_per_sol": p_post_trigger,
            "pre_sol_per_ray": ray_sol_pre,
            "post_trigger_sol_per_ray": ray_sol_post_trigger,
            "ray_price_impact_bps": (ray_sol_post_trigger / ray_sol_pre - Decimal(1)) * Decimal(10000),
        },
        "bot_clmm_repair": {
            "same_pool_state": bev["pool_state"],
            "bot_pre_sqrt_from_swap_math": bot_pre_sqrt,
            "trigger_post_sqrt": Decimal(tev["sqrt_price_x64"]),
            "sqrt_match_error_q64_units": bot_pre_sqrt - Decimal(tev["sqrt_price_x64"]),
            "post_bot_sqrt_price_x64": bev["sqrt_price_x64"],
            "post_bot_tick": bev["tick"],
            "post_bot_ray_per_sol": p_post_bot,
            "post_bot_sol_per_ray": ray_sol_post_bot,
            "repair_bps_in_ray_price": (ray_sol_post_bot / ray_sol_post_trigger - Decimal(1)) * Decimal(10000),
            "residual_distortion_bps_vs_pre": (ray_sol_post_bot / ray_sol_pre - Decimal(1)) * Decimal(10000),
        },
        "bot_route": leg_summaries,
        "bot_economics": {
            "initial_usdc": initial_usdc,
            "final_usdc": final_usdc,
            "gross_profit_usdc": gross_profit,
            "gross_bps": gross_bps,
            "amm_v4_buy_price_usdc_per_ray": amm_usdc_per_ray,
            "clmm_implied_sell_price_usdc_per_ray": implied_clmm_usdc_per_ray,
            "cross_pool_edge_bps": (implied_clmm_usdc_per_ray / amm_usdc_per_ray - Decimal(1)) * Decimal(10000),
            "sol_usdc_execution_price": sol_usdc_exec,
            "jito_tip": tip,
            "jito_tip_usdc_equivalent": tip_usdc,
            "tx_fee_lamports": base_fee_lamports,
            "tx_fee_usdc_equivalent": base_fee_usdc,
            "net_profit_after_jito_and_tx_fee_usdc": net_profit_after_tip_fee,
            "net_bps_after_jito_and_tx_fee": net_bps_after_tip_fee,
        },
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reconstruction.json").write_text(json.dumps(jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")

    def fmt(x: Decimal, n: int = 9) -> str:
        return f"{x:.{n}f}"

    md = [
        "# RAY clean event reconstruction",
        "",
        f"- Ledger: `{slot}:{trigger_idx}` trigger → `{slot}:{bot_idx}` MRiYA4 (delta={bot_idx-trigger_idx})",
        f"- Trigger CLMM pool: `{tev['pool_state']}`",
        "",
        "## Trigger price shock",
        f"- Pre: {fmt(ray_sol_pre, 12)} SOL/RAY ({fmt(p_pre, 6)} RAY/SOL)",
        f"- Post trigger: {fmt(ray_sol_post_trigger, 12)} SOL/RAY ({fmt(p_post_trigger, 6)} RAY/SOL)",
        f"- RAY price impact: {fmt(report['trigger_clmm']['ray_price_impact_bps'], 3)} bps",
        f"- Constant-liquidity reconstruction check error: {fmt(inferred['amount0_consistency_error_bps'], 6)} bps",
        "",
        "## MRiYA4 route",
    ]
    for i, leg in enumerate(leg_summaries, 1):
        md.append(
            f"{i}. {leg['venue']} pool `{leg['pool_state']}`: "
            f"{leg['amount_in']} {leg['mint_in']} → {leg['amount_out']} {leg['mint_out']}"
        )
    md += [
        "",
        "## Same-pool repair",
        f"- CLMM post-trigger: {fmt(ray_sol_post_trigger, 12)} SOL/RAY",
        f"- CLMM post-bot: {fmt(ray_sol_post_bot, 12)} SOL/RAY",
        f"- Bot repaired: {fmt(report['bot_clmm_repair']['repair_bps_in_ray_price'], 3)} bps of RAY price",
        f"- Residual vs pre-trigger: {fmt(report['bot_clmm_repair']['residual_distortion_bps_vs_pre'], 3)} bps",
        f"- Bot-derived CLMM pre-state vs trigger post-state sqrt error: {report['bot_clmm_repair']['sqrt_match_error_q64_units']} Q64 units",
        "",
        "## Bot economics",
        f"- USDC in: {initial_usdc}",
        f"- USDC out: {final_usdc}",
        f"- Gross profit: {gross_profit} USDC = {fmt(gross_bps, 3)} bps",
        f"- Raydium AMM v4 RAY buy: {fmt(amm_usdc_per_ray, 9)} USDC/RAY",
        f"- CLMM leg implied via Tessera: {fmt(implied_clmm_usdc_per_ray, 9)} USDC/RAY",
        f"- Jito tip: {tip['sol']} SOL ≈ {fmt(tip_usdc, 6)} USDC",
        f"- Solana tx fee: {base_fee_lamports} lamports ≈ {fmt(base_fee_usdc, 6)} USDC",
        f"- Net after Jito + tx fee: {fmt(net_profit_after_tip_fee, 6)} USDC = {fmt(net_bps_after_tip_fee, 3)} bps",
    ]
    (out_dir / "reconstruction.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print("\n".join(md))


if __name__ == "__main__":
    main()
