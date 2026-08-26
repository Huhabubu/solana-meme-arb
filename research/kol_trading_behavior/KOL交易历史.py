import requests
import time
import pandas as pd
from datetime import datetime, timedelta


def _tx_hash(trade):
    for key in ("txHash", "transactionHash", "transaction_hash", "hash"):
        value = trade.get(key)
        if value:
            return str(value)
    url = trade.get("txHashUrl")
    if url:
        return str(url).rstrip("/").split("/")[-1]
    return None


def get_trades_around_kol_fixed(
    token_address,
    kol_address,
    target_time_str=None,
    chain_id="501",
    target_time_ms=None,
    verbose=False,
    before_seconds=30,
    after_seconds=20,
    max_pages=100,
):
    """
    获取 KOL 事件附近的 Mint 全市场逐笔成交。

    GitHub Runner 2026-08-26 已实测：
    - tradingHistoryFilter.startTime/endTime 会在服务端按时间过滤；
    - dataId 可以直接使用 epoch 毫秒时间戳定位；
    - 单页有效上限为 100。

    因此这里不再从“最新成交”一路向过去翻页，而是直接定位到事件窗口。
    返回仍保持原 DataFrame 展示列，并额外保留 txHash、完整性与页数属性。
    """
    if target_time_ms is not None:
        t0_ms = int(target_time_ms)
        target_time_display = datetime.fromtimestamp(t0_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    else:
        if not target_time_str:
            raise ValueError("target_time_ms 与 target_time_str 至少提供一个")
        target_dt = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
        t0_ms = int(target_dt.timestamp() * 1000)
        target_time_display = target_time_str

    start_ts = t0_ms - int(before_seconds * 1000)
    end_ts = t0_ms + int(after_seconds * 1000)

    url = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Referer": "https://web3.okx.com/zh-hans/market/dex",
    }

    all_matched_trades = []
    current_id = str(end_ts)  # 直接定位到窗口末端
    pages = 0
    complete = False

    if verbose:
        print(
            f"分析窗口: {target_time_display} "
            f"(-{before_seconds}s ~ +{after_seconds}s)，使用时间戳直达分页"
        )

    session = requests.Session()

    while pages < max_pages:
        payload = {
            "desc": True,
            "orderBy": "timestamp",
            "limit": 100,
            "dataId": current_id,
            "tradingHistoryFilter": {
                "chainId": str(chain_id),
                "tokenContractAddress": token_address,
                "type": "0",
                "currentUserWalletAddress": "",
                "userAddressList": [],
                "volumeMin": "",
                "volumeMax": "",
                "priceMin": "",
                "priceMax": "",
                "amountMin": "",
                "amountMax": "",
                "startTime": start_ts,
                "endTime": end_ts,
            },
        }

        try:
            resp = session.post(
                f"{url}?t={int(time.time() * 1000)}",
                json=payload,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            res_json = resp.json()
            if str(res_json.get("code")) != "0":
                raise RuntimeError(
                    f"OKX code={res_json.get('code')} msg={res_json.get('msg')}"
                )

            data_obj = res_json.get("data", {}) or {}
            trade_list = data_obj.get("list", []) or []
            pages += 1

            if not trade_list:
                complete = True
                break

            oldest_ts = min(int(x.get("timestamp", 0) or 0) for x in trade_list)
            if verbose:
                progress_time = datetime.fromtimestamp(oldest_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"\r>>> 第 {pages} 页，扫描到: {progress_time}",
                    end="",
                    flush=True,
                )

            for trade in trade_list:
                ts = int(trade.get("timestamp", 0) or 0)
                if not (start_ts <= ts <= end_ts):
                    continue

                user_addr = trade.get("userAddress") or ""
                is_kol = "★ KOL" if user_addr.lower() == kol_address.lower() else ""

                all_matched_trades.append(
                    {
                        "时间": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                        "交易类型": "买入" if trade.get("isBuy") == "1" else "卖出",
                        "USD数量": round(float(trade.get("volume", 0) or 0), 2),
                        "交易者地址": user_addr,
                        "KOL标记": is_kol,
                        "timestamp_ms": ts,
                        "dataId": trade.get("id"),
                        "txHash": _tx_hash(trade),
                        "price": trade.get("price"),
                        "token_address": token_address,
                        "kol_address": kol_address,
                        "chain_id": str(chain_id),
                    }
                )

            if oldest_ts <= start_ts or str(data_obj.get("hasMore", "0")) != "1":
                complete = True
                break

            next_id = trade_list[-1].get("id")
            if not next_id or str(next_id) == current_id:
                break
            current_id = str(next_id)
            time.sleep(0.01)

        except Exception as e:
            if verbose:
                print(f"\n请求遇到问题: {e}")
            break

    if verbose:
        print(f"\n完成: pages={pages}, rows={len(all_matched_trades)}, complete={complete}")

    if not all_matched_trades:
        df = pd.DataFrame()
    else:
        df = (
            pd.DataFrame(all_matched_trades)
            .drop_duplicates(subset=["dataId"], keep="last")
            .sort_values(by="timestamp_ms")
            .reset_index(drop=True)
        )

    # 不改变函数返回类型；完整性通过 DataFrame.attrs 附带。
    df.attrs["window_complete"] = complete
    df.attrs["pages"] = pages
    df.attrs["start_ts"] = start_ts
    df.attrs["end_ts"] = end_ts
    return df
