#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Corrected entry point for the clean RAY event price reconstruction.

The original forensic script relied on pre/postTokenBalances to infer decimals for
CLMM event token accounts. The trigger uses a temporary WSOL account that is
created/closed inside the transaction, so that metadata can be absent. This
entry point resolves Mint/decimals from parsed SPL transferChecked instructions
and then runs the same reconstruction logic.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

import ray_clean_event_price_reconstruct as core


def robust_event_mints_and_decimals(
    tx: Dict[str, Any], ev: Dict[str, Any]
) -> Tuple[str, int, str, int]:
    account0 = str(ev["token_account_0"])
    account1 = str(ev["token_account_1"])
    resolved: Dict[str, Dict[str, Any]] = {}

    # Seed from transaction token-balance metadata where available.
    for account, meta in core.token_account_meta(tx).items():
        if account in {account0, account1}:
            mint = str(meta.get("mint") or "")
            if mint:
                resolved[account] = {
                    "mint": mint,
                    "decimals": int(meta.get("decimals") or 0),
                    "source": "token_balance_metadata",
                }

    # transferChecked contains Mint + decimals and also works for temporary token
    # accounts that do not survive to postTokenBalances.
    for row in core.inner_instructions(tx):
        if row.get("program_id") != core.TOKEN_PROGRAM:
            continue
        parsed = row.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") != "transferChecked":
            continue
        info = parsed.get("info") or {}
        mint = str(info.get("mint") or "")
        token_amount = info.get("tokenAmount") or {}
        decimals = token_amount.get("decimals")
        if not mint or decimals is None:
            continue
        for account in (str(info.get("source") or ""), str(info.get("destination") or "")):
            if account in {account0, account1}:
                resolved[account] = {
                    "mint": mint,
                    "decimals": int(decimals),
                    "source": "transferChecked",
                }

    missing = [a for a in (account0, account1) if a not in resolved]
    if missing:
        raise RuntimeError(f"unable to resolve CLMM token Mint/decimals for {missing}")

    a = resolved[account0]
    b = resolved[account1]
    print(
        "resolved CLMM token metadata:",
        account0, a,
        account1, b,
    )
    return str(a["mint"]), int(a["decimals"]), str(b["mint"]), int(b["decimals"])


if __name__ == "__main__":
    core.event_mints_and_decimals = robust_event_mints_and_decimals
    core.main()
