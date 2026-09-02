#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize the clean RAY event as a second-order arbitrage-flow cluster.

Consumes outputs produced by:
- ray_clean_event_price_reconstruct_fixed.py
- ray_amm_second_order_probe.py
- ray_followon_pool_activity.py
- ray_followon_route_probe.py

The purpose is to measure the research hypothesis directly:
large trade shocks Pool A -> arbitrageurs repair Pool A by sourcing RAY elsewhere ->
those source-pool buys create a second-order price move in other pools.
"""
from __future__ import annotations

import json
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Dict

getcontext().prec = 50

BASE = Path("research/output/ray_clean_event_reconstruct")
D = Decimal


def load(name: str) -> Any:
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def dec(x: Any) -> Decimal:
    return D(str(x))


def bps(a: Decimal, b: Decimal) -> Decimal:
    """Move from a to b in bps."""
    return (b / a - D(1)) * D(10000)


def ceil_fraction(v: int, n: int, d: int) -> int:
    return (v * n + d - 1) // d


def quote_buy_ray(usdc_reserve: int, ray_reserve: int, usdc_in_ui: Decimal, fee_num: int, fee_den: int) -> Dict[str, Any]:
    amount_in = int(usdc_in_ui * D(1_000_000))
    fee = ceil_fraction(amount_in, fee_num, fee_den)
    net = amount_in - fee
    amount_out = ray_reserve * net // (usdc_reserve + net)
    ray_out = D(amount_out) / D(1_000_000)
    avg_price = usdc_in_ui / ray_out
    return {
        "usdc_in": str(usdc_in_ui),
        "ray_out": str(ray_out),
        "avg_usdc_per_ray": str(avg_price),
    }


def main() -> None:
    recon = load("reconstruction.json")
    amm = load("amm_second_order.json")
    follow = load("followon_pool_activity.json")
    routes = load("followon_route_probe.json")

    clmm_pre = dec(recon["trigger_clmm"]["pre_sol_per_ray"])
    clmm_post_trigger = dec(recon["trigger_clmm"]["post_trigger_sol_per_ray"])
    clmm_post_first_bot = dec(recon["bot_clmm_repair"]["post_bot_sol_per_ray"])

    follow_events = []
    follow_ray = D(0)
    signers = set()
    for tx in follow["transactions"]:
        signers.update(tx.get("signers") or [])
        for ev in tx.get("clmm_events") or []:
            ray_ui = D(ev["amount1_raw"]) / D(1_000_000)
            follow_ray += ray_ui
            follow_events.append({
                "index": int(tx["index"]),
                "signature": tx["signature"],
                "signer": (tx.get("signers") or [""])[0],
                "ray_sold_to_target_clmm": str(ray_ui),
                "sol_received_from_target_clmm": str(D(ev["amount0_raw"]) / D(1_000_000_000)),
                "post_sol_per_ray": str(ev["post_sol_per_ray"]),
            })

    if follow_events:
        clmm_post_cluster = dec(follow_events[-1]["post_sol_per_ray"])
    else:
        clmm_post_cluster = clmm_post_first_bot

    first_bot_ray = dec(recon["bot_route"][1]["amount_in"])
    total_repair_ray = first_bot_ray + follow_ray

    # Exact AMM-v4 standardized quote shift caused by MRiYA4's first-leg buy.
    hv = amm["historical_vaults"]
    state = amm["state"]
    pre_u = int(hv["usdc_pre_raw"])
    pre_r = int(hv["ray_pre_raw"])
    post_u = int(hv["usdc_post_raw"])
    post_r = int(hv["ray_post_raw"])
    fee_num = int(state["fee_num"])
    fee_den = int(state["fee_den"])
    quote_table = []
    for size in (D(100), D(500), D(1000), D(2000)):
        preq = quote_buy_ray(pre_u, pre_r, size, fee_num, fee_den)
        postq = quote_buy_ray(post_u, post_r, size, fee_num, fee_den)
        p0 = dec(preq["avg_usdc_per_ray"])
        p1 = dec(postq["avg_usdc_per_ray"])
        quote_table.append({
            "usdc_input": str(size),
            "pre_bot_ray_out": preq["ray_out"],
            "post_bot_ray_out": postq["ray_out"],
            "pre_bot_avg_usdc_per_ray": preq["avg_usdc_per_ray"],
            "post_bot_avg_usdc_per_ray": postq["avg_usdc_per_ray"],
            "execution_price_shift_bps": str(bps(p0, p1)),
        })

    # Route-program evidence for the five follow-on target-CLMM sells.
    program_counts: Dict[str, int] = {}
    known = {
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    }
    for route in routes:
        for pid in route.get("nonbasic_programs") or []:
            label = known.get(pid, pid)
            program_counts[label] = program_counts.get(label, 0) + 1

    result = {
        "event": {
            "slot": recon["event"]["slot"],
            "trigger_index": recon["event"]["trigger_index"],
            "first_bot_index": recon["event"]["bot_index"],
            "trigger_to_first_bot_index_delta": recon["event"]["index_delta"],
        },
        "primary_shock": {
            "pre_sol_per_ray": str(clmm_pre),
            "post_trigger_sol_per_ray": str(clmm_post_trigger),
            "shock_bps": str(bps(clmm_pre, clmm_post_trigger)),
        },
        "first_bot": {
            "ray_sold_to_target_clmm": str(first_bot_ray),
            "target_clmm_repair_bps": str(bps(clmm_post_trigger, clmm_post_first_bot)),
            "source_amm_v4_marginal_move_bps": amm["marginal_reserve_ratio_price"]["bot_induced_move_bps"],
            "source_amm_v4_pre_usdc_per_ray": amm["marginal_reserve_ratio_price"]["pre_usdc_per_ray"],
            "source_amm_v4_post_usdc_per_ray": amm["marginal_reserve_ratio_price"]["post_usdc_per_ray"],
            "source_amm_quote_reproduces_landed_swap_exactly": amm["quote_reproduction"]["exact_match"],
        },
        "follow_on_cluster": {
            "follow_on_transactions": len(follow["transactions"]),
            "distinct_follow_on_signers": len(signers),
            "additional_ray_sold_to_target_clmm": str(follow_ray),
            "total_ray_sold_to_target_clmm_including_first_bot": str(total_repair_ray),
            "additional_repair_bps_after_first_bot": str(bps(clmm_post_first_bot, clmm_post_cluster)),
            "total_repair_bps_from_post_trigger": str(bps(clmm_post_trigger, clmm_post_cluster)),
            "residual_distortion_bps_vs_pre_trigger": str(bps(clmm_pre, clmm_post_cluster)),
            "events": follow_events,
            "route_program_presence_counts": program_counts,
        },
        "standardized_amm_v4_buy_quotes": quote_table,
        "interpretation": {
            "second_order_effect_confirmed_in_this_sample": True,
            "reason": (
                "The first backrun bought RAY in a separate RAY/USDC AMM v4 pool, "
                "moving that pool's marginal RAY price upward, while later same-slot "
                "transactions continued selling RAY into the originally shocked CLMM."
            ),
            "caution": (
                "This is one labeled event. The AMM v4 fee is 25 bps, so an observed ~11 bps "
                "source-pool move is not by itself enough for a naive same-pool round trip; "
                "the research target is cross-venue execution / lower-cost exit and whether the "
                "effect repeats across many events."
            ),
        },
    }

    (BASE / "second_order_cluster_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# RAY second-order arbitrage cluster",
        "",
        f"- Trigger shock: **{dec(result['primary_shock']['shock_bps']):.3f} bps**",
        f"- First MRiYA4 source-pool move (RAY/USDC AMM v4): **{dec(result['first_bot']['source_amm_v4_marginal_move_bps']):.3f} bps**",
        f"- First MRiYA4 target-CLMM repair: **{dec(result['first_bot']['target_clmm_repair_bps']):.3f} bps**",
        f"- Follow-on target-CLMM transactions: **{result['follow_on_cluster']['follow_on_transactions']}** from **{result['follow_on_cluster']['distinct_follow_on_signers']}** signers",
        f"- Additional RAY sold into target CLMM: **{dec(result['follow_on_cluster']['additional_ray_sold_to_target_clmm']):.6f} RAY**",
        f"- Total RAY sold into target CLMM after trigger: **{dec(result['follow_on_cluster']['total_ray_sold_to_target_clmm_including_first_bot']):.6f} RAY**",
        f"- Total target-CLMM repair through end of observed same-slot cluster: **{dec(result['follow_on_cluster']['total_repair_bps_from_post_trigger']):.3f} bps**",
        f"- Residual target-CLMM distortion vs pre-trigger: **{dec(result['follow_on_cluster']['residual_distortion_bps_vs_pre_trigger']):.3f} bps**",
        "",
        "## Standardized RAY/USDC AMM v4 buy quote shift",
        "",
        "| USDC input | pre-bot avg USDC/RAY | post-bot avg USDC/RAY | shift bps |",
        "|---:|---:|---:|---:|",
    ]
    for q in quote_table:
        lines.append(
            f"| {dec(q['usdc_input']):.0f} | {dec(q['pre_bot_avg_usdc_per_ray']):.9f} | "
            f"{dec(q['post_bot_avg_usdc_per_ray']):.9f} | {dec(q['execution_price_shift_bps']):.3f} |"
        )
    lines += [
        "",
        "## Follow-on target CLMM sells",
        "",
        "| index | RAY sold | SOL received | post SOL/RAY |",
        "|---:|---:|---:|---:|",
    ]
    for e in follow_events:
        lines.append(
            f"| {e['index']} | {dec(e['ray_sold_to_target_clmm']):.6f} | "
            f"{dec(e['sol_received_from_target_clmm']):.9f} | {dec(e['post_sol_per_ray']):.12f} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "This sample confirms the mechanism we care about: the large trade shocks the RAY/SOL CLMM, "
        "the first arbitrageur buys RAY in another pool and thereby moves that source pool, and additional "
        "same-slot flow continues repairing the shocked pool.",
        "",
        "The first observed source-pool move is about 11 bps, below the 25 bps AMM-v4 fee per swap. "
        "So this does **not** support a naive same-pool buy-then-sell strategy. It supports testing whether "
        "the induced move can be captured with a cheaper/cross-venue exit and whether the magnitude repeats across events.",
    ]
    (BASE / "second_order_cluster_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
