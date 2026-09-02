#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure the corrected passive-capture condition for SLIM.

For a SELL shock in A, our passive bid in each destination B should only fill if
arb sell-flow pushes B down to/below the maximum bid price that can still be
exited profitably into A. This script compares:

    observed B post-trade marginal reserve price
vs
    A exact-input sell value per SLIM (our breakeven passive bid)

If B_post > breakeven_bid, then even the observed arb sell did not push B deep
enough to touch a profitable passive bid. This is a stronger and more faithful
criterion than pretending we absorb the arb's whole B trade at average price.

Maker execution is still not proven: B1 is Orca legacy token-swap and B3 is
Raydium AMM-v4. This is an economic crossing test only.
"""
from __future__ import annotations

import json
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 60

# A Raydium AMM-v4 static parameters recovered from state.
SLIM_PNL=233_837_112
SOL_PNL=9_703_578
FEE_NUM=25
FEE_DEN=10_000

# Exact A post-transaction states.
A_STATES={
    "B1": {"slim_raw":40_229_970_153_466,"sol_raw":1_805_402_508_082},  # after index 161
    "B3": {"slim_raw":40_229_810_784_190,"sol_raw":1_805_409_678_063},  # after index 738
}

# B1 exact post reserves from index 161.
B1_POST_SLIM=2_912_003_781_395
B1_POST_USDC=13_366_461_452
# B3 exact post reserves from index 738.
B3_POST_SLIM=5_570_833_307
B3_POST_USDC=25_699_618

# Executable USDC/SOL references internal to the same arb transaction.
# B1 route: 12.987452 USDC -> 0.127619177 SOL.
B1_SOLUSD=Decimal("12.987452")/Decimal("0.127619177")
# B3 route: 0.231735 USDC -> 0.002278287 SOL.
B3_SOLUSD=Decimal("0.231735")/Decimal("0.002278287")


def ui(raw:int,dec:int)->Decimal:
    return Decimal(raw)/(Decimal(10)**dec)


def quote_a_sell(slim_raw:int,sol_raw:int,qty_slim_raw:int)->int:
    fee=(qty_slim_raw*FEE_NUM+FEE_DEN-1)//FEE_DEN
    net=qty_slim_raw-fee
    return (sol_raw-SOL_PNL)*net//((slim_raw-SLIM_PNL)+net)


def run_case(name:str,b_post_slim:int,b_post_usdc:int,solusd:Decimal,sizes:list[Decimal])->dict:
    a=A_STATES[name]
    b_post=ui(b_post_usdc,6)/ui(b_post_slim,6)
    rows=[]
    for q in sizes:
        qraw=int(q*Decimal(1_000_000))
        out_sol=ui(quote_a_sell(a["slim_raw"],a["sol_raw"],qraw),9)
        exit_usdc=out_sol*solusd
        bid_break_even=exit_usdc/q
        # Positive means B remained ABOVE our profitable bid after the arb sale.
        gap_bps=(b_post/bid_break_even-Decimal(1))*Decimal(10_000)
        # How much lower B would still need to move from observed post state.
        extra_drop_bps=(Decimal(1)-bid_break_even/b_post)*Decimal(10_000)
        rows.append({
            "size_slim":str(q),
            "A_exit_sol":str(out_sol),
            "A_exit_value_usdc":str(exit_usdc),
            "max_profitable_B_bid_usdc_per_slim":str(bid_break_even),
            "observed_B_post_marginal_usdc_per_slim":str(b_post),
            "B_post_above_profitable_bid_bps":str(gap_bps),
            "additional_B_drop_needed_bps":str(extra_drop_bps),
            "profitable_bid_was_crossed":bool(b_post<=bid_break_even),
        })
    return {"case":name,"same_tx_solusd":str(solusd),"B_post_marginal":str(b_post),"sizes":rows}


def main()->None:
    result={
        "definition":"profitable passive bid is touched only when observed B marginal price <= exact A exit value per token",
        "maker_executability_proven":False,
        "B1":run_case("B1",B1_POST_SLIM,B1_POST_USDC,B1_SOLUSD,[Decimal("1"),Decimal("10"),Decimal("100"),Decimal("500"),Decimal("1000"),Decimal("2836.841021")]),
        "B3":run_case("B3",B3_POST_SLIM,B3_POST_USDC,B3_SOLUSD,[Decimal("1"),Decimal("10"),Decimal("50.179675"),Decimal("100"),Decimal("500"),Decimal("1000")]),
    }
    out=Path("research/output/passive_capture/slim_profitable_bid_crossing.json")
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("# SLIM profitable passive-bid crossing")
    for key in ["B1","B3"]:
        r=result[key]
        print("\n",key,"B post marginal",r["B_post_marginal"],"same-tx SOLUSD",r["same_tx_solusd"])
        for x in r["sizes"]:
            print(x["size_slim"],"SLIM | max bid",x["max_profitable_B_bid_usdc_per_slim"],
                  "| B post above bid",x["B_post_above_profitable_bid_bps"],"bps | crossed",x["profitable_bid_was_crossed"])

if __name__=="__main__":main()
