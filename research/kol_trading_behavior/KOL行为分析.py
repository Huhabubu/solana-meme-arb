# -*- coding: utf-8 -*-
"""
KOL 交易行为分析工具。

给定一个 Solana 钱包地址：
1. 获取最近 N 笔买卖记录；
2. 按 Token 还原样本内交易序列；
3. 检测“大额样本首买 -> 后续小额补买/试探 -> 分批/大额卖出”；
4. 可选检查小额补买前 30 秒、后 20 秒的市场买入响应。

注意：最近 N 笔是截断样本，所以“样本首笔买入”不等于历史真实首次建仓。
"""

from __future__ import annotations

import time
from typing import Dict, List

import pandas as pd
import requests

from KOL交易历史 import get_trades_around_kol_fixed


WALLET_TRADE_URL = (
    "https://web3.okx.com/priapi/v1/dx/market/v2/pnl/wallet-profile/trade-history"
)


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def fetch_recent_wallet_trades(
    wallet_address: str,
    n: int = 200,
    chain_id: int = 501,
    verbose: bool = False,
) -> pd.DataFrame:
    """获取地址最近 N 笔交易，结果按时间从新到旧排列。"""
    if not wallet_address:
        raise ValueError("wallet_address 不能为空")
    if n <= 0:
        raise ValueError("n 必须大于 0")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
        "Accept": "application/json",
    }

    session = requests.Session()
    global_index = None
    block_time_pagination = None
    has_next = True
    result: List[Dict] = []
    seen = set()

    while has_next and len(result) < n:
        params = {
            "walletAddress": wallet_address,
            "chainId": chain_id,
            "pageSize": min(50, n - len(result)),
            "tradeType": "1,2",
            "filterRisk": "true",
            "t": int(time.time() * 1000),
        }
        if global_index is not None and block_time_pagination is not None:
            params["globalIndex"] = global_index
            params["blockTimePagination"] = block_time_pagination

        response = session.get(WALLET_TRADE_URL, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()

        if str(payload.get("code", "0")) != "0":
            raise RuntimeError(
                f"OKX 接口错误: {payload.get('code')} {payload.get('msg')}"
            )

        data = payload.get("data") or {}
        rows = data.get("rows") or []
        raw_has_next = data.get("hasNext", False)
        has_next = (
            raw_has_next.strip().lower() in {"1", "true", "yes"}
            if isinstance(raw_has_next, str)
            else bool(raw_has_next)
        )

        if not rows:
            break

        for trade in rows:
            token = trade.get("tokenContractAddress")
            side_code = _int(trade.get("type"))
            block_time_ms = _int(trade.get("blockTime"))
            if not token or side_code not in (1, 2):
                continue

            key = (
                trade.get("txHash"),
                token,
                side_code,
                block_time_ms,
                trade.get("globalIndex"),
            )
            if key in seen:
                continue
            seen.add(key)

            amount = _float(trade.get("amount"))
            price = _float(trade.get("price"))
            result.append(
                {
                    "kol_address": wallet_address,
                    "symbol": trade.get("tokenSymbol"),
                    "token_address": token,
                    "side": "BUY" if side_code == 1 else "SELL",
                    "amount": amount,
                    "price": price,
                    "total_value_usd": amount * price,
                    "blockTime_ms": block_time_ms,
                    "time": (
                        time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.localtime(block_time_ms / 1000),
                        )
                        if block_time_ms
                        else None
                    ),
                    "txHash": trade.get("txHash"),
                    "globalIndex": trade.get("globalIndex"),
                }
            )
            if len(result) >= n:
                break

        last = rows[-1]
        next_global_index = last.get("globalIndex")
        next_block_time = last.get("blockTime")
        if (
            has_next
            and next_global_index == global_index
            and next_block_time == block_time_pagination
        ):
            break

        global_index = next_global_index
        block_time_pagination = next_block_time

        if verbose:
            print(f"已获取 {len(result)}/{n} 笔")
        if has_next and len(result) < n:
            time.sleep(0.2)

    if not result:
        return pd.DataFrame()

    return (
        pd.DataFrame(result)
        .sort_values("blockTime_ms", ascending=False)
        .head(n)
        .reset_index(drop=True)
    )


def summarize_trade_patterns(
    trades: pd.DataFrame,
    probe_buy_ratio: float = 0.25,
    large_first_buy_multiple: float = 3.0,
    large_sell_ratio: float = 0.75,
    staged_sell_ratio: float = 0.50,
) -> pd.DataFrame:
    """
    按 Token 检查用户提出的行为假设。

    probe_buy_ratio=0.25:
        后续买入 <= 样本首笔买入的 25%，视为“小额补买/试探候选”。

    large_first_buy_multiple=3:
        样本首笔买入 >= 后续买入中位数的 3 倍，视为“首笔相对大额”。

    large_sell_ratio=0.75:
        后续单笔卖出 >= 样本首笔买入的 75%，视为“大额卖出候选”。
    """
    if trades.empty:
        return pd.DataFrame()

    out = []

    for token, token_df in trades.groupby("token_address", sort=False):
        g = token_df.sort_values("blockTime_ms").reset_index(drop=True)
        buys = g[g["side"] == "BUY"]
        if buys.empty:
            continue

        first_buy_idx = buys.index[0]
        first_buy = g.loc[first_buy_idx]
        first_usd = _float(first_buy["total_value_usd"])

        later = g.loc[first_buy_idx + 1 :]
        later_buys = later[later["side"] == "BUY"]
        later_sells = later[later["side"] == "SELL"]

        buy_median = (
            float(later_buys["total_value_usd"].median())
            if not later_buys.empty
            else None
        )
        sell_median = (
            float(later_sells["total_value_usd"].median())
            if not later_sells.empty
            else None
        )

        probes = later_buys[
            later_buys["total_value_usd"] <= first_usd * probe_buy_ratio
        ]
        large_sells = later_sells[
            later_sells["total_value_usd"] >= first_usd * large_sell_ratio
        ]

        first_is_large = bool(
            buy_median is not None
            and buy_median > 0
            and first_usd >= buy_median * large_first_buy_multiple
        )
        staged_sell = bool(
            len(later_sells) >= 2
            and sell_median is not None
            and sell_median <= first_usd * staged_sell_ratio
        )
        large_sell_after_probe = bool(
            not probes.empty
            and not large_sells.empty
            and (
                large_sells["blockTime_ms"]
                > int(probes["blockTime_ms"].min())
            ).any()
        )

        if first_is_large and not probes.empty and large_sell_after_probe:
            pattern = "大额样本首买→小额补买/试探→后续大额卖出"
        elif first_is_large and not probes.empty and staged_sell:
            pattern = "大额样本首买→小额补买/试探→分批卖出"
        elif first_is_large and staged_sell:
            pattern = "大额样本首买→分批卖出"
        else:
            pattern = "未命中当前模式"

        out.append(
            {
                "symbol": first_buy.get("symbol"),
                "token_address": token,
                "sample_trade_count": len(g),
                "sample_first_event": g.iloc[0]["side"],
                "sample_first_buy_time": first_buy.get("time"),
                "sample_first_buy_usd": first_usd,
                "later_buy_count": len(later_buys),
                "later_buy_median_usd": buy_median,
                "first_vs_later_buy_median": (
                    first_usd / buy_median
                    if buy_median is not None and buy_median > 0
                    else None
                ),
                "probe_buy_count": len(probes),
                "sell_count_after_first_buy": len(later_sells),
                "sell_median_usd": sell_median,
                "large_sell_count": len(large_sells),
                "first_buy_relative_large": first_is_large,
                "staged_sell_candidate": staged_sell,
                "large_sell_after_probe": large_sell_after_probe,
                "pattern": pattern,
                "historical_first_buy_verified": False,
            }
        )

    return pd.DataFrame(out)


def _follow_response(window: pd.DataFrame, kol_address: str, t0_ms: int) -> Dict:
    """统计 KOL 交易前后其他地址的买入强度。"""
    if window is None or window.empty:
        return {
            "pre_buy_count": 0,
            "pre_unique_buyers": 0,
            "pre_buy_usd": 0.0,
            "post_buy_count": 0,
            "post_unique_buyers": 0,
            "post_buy_usd": 0.0,
            "buy_count_response_ratio": None,
            "buy_usd_response_ratio": None,
            "follow_response_candidate": False,
        }

    market = window[
        window["交易者地址"].fillna("").str.lower() != kol_address.lower()
    ]
    pre = market[
        (market["timestamp_ms"] < t0_ms) & (market["交易类型"] == "买入")
    ]
    post = market[
        (market["timestamp_ms"] > t0_ms) & (market["交易类型"] == "买入")
    ]

    pre_count = len(pre)
    post_count = len(post)
    pre_usd = float(pre["USD数量"].sum()) if not pre.empty else 0.0
    post_usd = float(post["USD数量"].sum()) if not post.empty else 0.0

    # 原始窗口是前 30 秒、后 20 秒，所以比较每秒强度。
    pre_count_rate = pre_count / 30.0
    post_count_rate = post_count / 20.0
    pre_usd_rate = pre_usd / 30.0
    post_usd_rate = post_usd / 20.0

    count_ratio = (post_count_rate + 1e-9) / (pre_count_rate + 1e-9)
    usd_ratio = (post_usd_rate + 1e-9) / (pre_usd_rate + 1e-9)

    return {
        "pre_buy_count": pre_count,
        "pre_unique_buyers": pre["交易者地址"].nunique(),
        "pre_buy_usd": pre_usd,
        "post_buy_count": post_count,
        "post_unique_buyers": post["交易者地址"].nunique(),
        "post_buy_usd": post_usd,
        "buy_count_response_ratio": count_ratio,
        "buy_usd_response_ratio": usd_ratio,
        "follow_response_candidate": bool(
            post_count >= 2 and count_ratio >= 1.5 and usd_ratio >= 1.5
        ),
    }


def inspect_probe_buys(
    trades: pd.DataFrame,
    kol_address: str,
    probe_buy_ratio: float = 0.25,
    max_probes: int = 20,
    chain_id: str = "501",
    verbose: bool = False,
) -> pd.DataFrame:
    """
    找出后续小额买入候选，并检查其后是否出现跟单买入以及 KOL 后续卖出。
    """
    if trades.empty:
        return pd.DataFrame()

    probes = []

    for _, token_df in trades.groupby("token_address", sort=False):
        g = token_df.sort_values("blockTime_ms").reset_index(drop=True)
        buys = g[g["side"] == "BUY"]
        if buys.empty:
            continue

        first_buy_idx = buys.index[0]
        first_usd = _float(g.loc[first_buy_idx, "total_value_usd"])
        if first_usd <= 0:
            continue

        later_buys = g.loc[first_buy_idx + 1 :]
        later_buys = later_buys[
            (later_buys["side"] == "BUY")
            & (later_buys["total_value_usd"] <= first_usd * probe_buy_ratio)
        ]
        for _, probe in later_buys.iterrows():
            probes.append((probe, first_usd))

    probes.sort(key=lambda x: int(x[0]["blockTime_ms"]), reverse=True)
    out = []

    for probe, first_usd in probes[:max_probes]:
        token = probe["token_address"]
        t0_ms = int(probe["blockTime_ms"])

        try:
            window = get_trades_around_kol_fixed(
                token_address=token,
                kol_address=kol_address,
                target_time_ms=t0_ms,
                chain_id=chain_id,
                verbose=verbose,
            )
            response = _follow_response(window, kol_address, t0_ms)
            error = None
        except Exception as exc:
            response = _follow_response(pd.DataFrame(), kol_address, t0_ms)
            error = str(exc)

        next_sells = trades[
            (trades["token_address"] == token)
            & (trades["side"] == "SELL")
            & (trades["blockTime_ms"] > t0_ms)
        ].sort_values("blockTime_ms")

        if next_sells.empty:
            next_sell_usd = None
            next_sell_delay_sec = None
            next_sell_vs_probe = None
            next_sell_vs_first = None
        else:
            next_sell = next_sells.iloc[0]
            next_sell_usd = _float(next_sell["total_value_usd"])
            next_sell_delay_sec = (
                int(next_sell["blockTime_ms"]) - t0_ms
            ) / 1000.0
            probe_usd = _float(probe["total_value_usd"])
            next_sell_vs_probe = (
                next_sell_usd / probe_usd if probe_usd > 0 else None
            )
            next_sell_vs_first = (
                next_sell_usd / first_usd if first_usd > 0 else None
            )

        out.append(
            {
                "symbol": probe.get("symbol"),
                "token_address": token,
                "probe_time": probe.get("time"),
                "probe_buy_usd": _float(probe["total_value_usd"]),
                "sample_first_buy_usd": first_usd,
                **response,
                "next_sell_delay_sec": next_sell_delay_sec,
                "next_sell_usd": next_sell_usd,
                "next_sell_vs_probe": next_sell_vs_probe,
                "next_sell_vs_first_buy": next_sell_vs_first,
                "probe_follow_then_large_exit_candidate": bool(
                    response["follow_response_candidate"]
                    and next_sell_vs_first is not None
                    and next_sell_vs_first >= 0.75
                ),
                "error": error,
            }
        )

    return pd.DataFrame(out)


def analyze_kol_address(
    wallet_address: str,
    n: int = 200,
    inspect_following: bool = True,
    max_probes: int = 20,
):
    """
    一站式入口。

    返回:
        recent_trades: 最近 N 笔交易
        token_summary: 各 Token 行为模式
        probe_response: 小额补买后的市场响应与后续卖出
    """
    recent_trades = fetch_recent_wallet_trades(wallet_address, n=n)
    token_summary = summarize_trade_patterns(recent_trades)
    probe_response = (
        inspect_probe_buys(
            recent_trades,
            kol_address=wallet_address,
            max_probes=max_probes,
        )
        if inspect_following
        else pd.DataFrame()
    )
    return recent_trades, token_summary, probe_response
