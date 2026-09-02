#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Decode the two later same-slot transactions that buy SLIM from shocked pool A.

Purpose: determine which destination venue/pool each later arbitrageur uses after
buying SLIM from A. This tests whether arb sell pressure concentrates in the
first observed B or fragments across multiple B_j venues.
"""
from __future__ import annotations

import json
from pathlib import Path

from passive_capture_event_probe import summarize_tx

SLIM="xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
CASES=[
    (735,"5wsGYRDV5GdpipTRW2Bf1FuXngGfPovbzaVdJYJerzet7HSNeT7iau9T1ffe2fsfBcGKVEUTLmWXBWTcjDwTAeJn"),
    (738,"2Pupf3odD6Vjn29QyDGyB7CPfVdYETywt2FxY9Uzf1SNzSdSLqgNFFjw9Xta6v4tax2oeU4Ma2q2Y7i27Ty5qmRj"),
]

def main()->None:
    out=[]
    for expected_idx,sig in CASES:
        tx=summarize_tx(sig,SLIM)
        if tx["index"]!=expected_idx:
            raise RuntimeError(f"index mismatch {sig}: {tx['index']} != {expected_idx}")
        out.append(tx)
        print(f"\n=== index {expected_idx} {sig} ===")
        print("signers",tx["signers"])
        print("programs",tx["nonbasic_programs"])
        print("target SLIM deltas")
        for x in tx["target_mint_net_deltas"]:print(" ",x)
        print("all token deltas")
        for x in tx["all_net_token_deltas"]:print(" ",x)
        print("candidate program states")
        for x in tx["candidate_program_state_accounts"]:print(" ",x)
        print("transfer groups")
        for x in tx["transfer_groups"]:print(" ",x)
    p=Path("research/output/passive_capture/slim_followon_destinations.json")
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__":main()
