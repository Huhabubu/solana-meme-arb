#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Decode SLIM arb routes at index 738 and next-slot 665/666.

Also verify whether the second Raydium AMM-v4 state in index 738 is the SLIM/USDC
sell destination B3 by decoding its current static pair/vault mapping and checking
historical transaction vault deltas.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from passive_capture_event_probe import summarize_tx

SLIM="xxxxa1sKNGwFtw2kFn8XauW9xq8hBZ5kVtcSesTT9fW"
USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL="So11111111111111111111111111111111111111112"
B3_STATE="ABrn4ED4AvkQ79VAXqf7ooqicJPHhZDAbC9rqcQ8ePzz"
CASES=[
 (443464889,738,"2Pupf3odD6Vjn29QyDGyB7CPfVdYETywt2FxY9Uzf1SNzSdSLqgNFFjw9Xta6v4tax2oeU4Ma2q2Y7i27Ty5qmRj"),
 (443464891,665,"66QzZS7X24WENwyGDYi5DfFofyJfxUJPnD2RB15j3u7CEuTNBp7ybNN7E5nJpunBibDW3upjegpKJceQ6X8szVp7"),
 (443464891,666,"ZYamvbhvLmMmbM1i2jrMdGsRjyffz5hjrGnujn5kx43uyTBiWxxPQw4sPB3RivYSJw2ypd2RxYu1FktfebXh1G7"),
]
COIN_VAULT_OFFSET=336;PC_VAULT_OFFSET=368;COIN_MINT_OFFSET=400;PC_MINT_OFFSET=432
COIN_DECIMALS_OFFSET=32;PC_DECIMALS_OFFSET=40;SWAP_FEE_NUMERATOR_OFFSET=176;SWAP_FEE_DENOMINATOR_OFFSET=184
B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58encode(raw:bytes)->str:
 n=int.from_bytes(raw,"big");out=""
 while n:n,r=divmod(n,58);out=B58[r]+out
 z=0
 for b in raw:
  if b==0:z+=1
  else:break
 return "1"*z+(out or "")
def u64(raw:bytes,o:int)->int:return int.from_bytes(raw[o:o+8],"little")
def pk(raw:bytes,o:int)->str:return b58encode(raw[o:o+32])
def rpc_url()->str:
 v=os.getenv("HELIUS_RPC_URL","").strip()
 if v:return v
 k=os.getenv("HELIUS_API_KEY","").strip()
 if k:return f"https://mainnet.helius-rpc.com/?api-key={k}"
 raise SystemExit("HELIUS_RPC_URL or HELIUS_API_KEY required")
def rpc(method:str,params:List[Any])->Any:
 data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode();req=urllib.request.Request(rpc_url(),data=data,headers={"content-type":"application/json"})
 with urllib.request.urlopen(req,timeout=90) as r:body=json.loads(r.read().decode())
 if body.get("error"):raise RuntimeError(body["error"])
 return body.get("result")
def get_tx(sig:str)->Dict[str,Any]:return rpc("getTransaction",[sig,{"commitment":"finalized","encoding":"jsonParsed","maxSupportedTransactionVersion":0}])
def keys(tx:Dict[str,Any])->List[str]:
 return [str(x if isinstance(x,str) else x.get("pubkey") or "") for x in tx["transaction"]["message"].get("accountKeys") or []]
def bal(tx:Dict[str,Any],pubkey:str)->Dict[str,Any]:
 ks=keys(tx);i=ks.index(pubkey);meta=tx.get("meta") or {};pre={int(x["accountIndex"]):x for x in meta.get("preTokenBalances") or []};post={int(x["accountIndex"]):x for x in meta.get("postTokenBalances") or []};a,b=pre.get(i,{}),post.get(i,{});ref=b or a
 def raw(v):return int(((v.get("uiTokenAmount") or {}).get("amount") or 0))
 return {"mint":ref.get("mint"),"decimals":int(((ref.get("uiTokenAmount") or {}).get("decimals") or 0)),"pre_raw":raw(a),"post_raw":raw(b),"delta_raw":raw(b)-raw(a)}
def decode_amm_state(pubkey:str)->Dict[str,Any]:
 v=rpc("getAccountInfo",[pubkey,{"encoding":"base64","commitment":"finalized"}])["value"];raw=base64.b64decode(v["data"][0])
 return {"pool":pubkey,"coin_decimals":u64(raw,COIN_DECIMALS_OFFSET),"pc_decimals":u64(raw,PC_DECIMALS_OFFSET),"fee_num":u64(raw,SWAP_FEE_NUMERATOR_OFFSET),"fee_den":u64(raw,SWAP_FEE_DENOMINATOR_OFFSET),"coin_vault":pk(raw,COIN_VAULT_OFFSET),"pc_vault":pk(raw,PC_VAULT_OFFSET),"coin_mint":pk(raw,COIN_MINT_OFFSET),"pc_mint":pk(raw,PC_MINT_OFFSET)}
def main()->None:
 st=decode_amm_state(B3_STATE);print("B3 state",st)
 tx738=get_tx(CASES[0][2]);vaults={v:bal(tx738,v) for v in [st["coin_vault"],st["pc_vault"]] if v in keys(tx738)};print("B3 historical vault deltas",vaults)
 out={"B3_state":st,"B3_vault_deltas_index738":vaults,"routes":[]}
 for slot,idx,sig in CASES:
  x=summarize_tx(sig,SLIM);out["routes"].append({"expected_slot":slot,"expected_index":idx,**x})
  print(f"\n=== {slot}:{idx} ===");print("programs",x["nonbasic_programs"]);print("target",x["target_mint_net_deltas"]);print("all deltas")
  for r in x["all_net_token_deltas"]:print(" ",r)
  print("states")
  for r in x["candidate_program_state_accounts"]:print(" ",r)
  print("transfers")
  for r in x["transfer_groups"]:print(" ",r)
 p=Path("research/output/passive_capture/slim_later_routes.json");p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
if __name__=="__main__":main()
