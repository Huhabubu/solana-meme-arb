import requests
import time
import pandas as pd
from datetime import datetime, timedelta

def get_trades_around_kol_fixed(
    token_address,
    kol_address,
    target_time_str=None,
    chain_id="501",
    target_time_ms=None,
    verbose=False
):
    """
    优先使用 target_time_ms (epoch ms) 作为 t0；
    如果未提供，则用 target_time_str 解析（保持你原逻辑）。
    """

    # 1. 严格计算时间窗口
    if target_time_ms is not None:
        start_ts = int(target_time_ms - 30 * 1000)
        end_ts = int(target_time_ms + 20 * 1000)
        target_time_display = datetime.fromtimestamp(target_time_ms/1000).strftime("%Y-%m-%d %H:%M:%S")
    else:
        target_dt = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
        start_ts = int((target_dt - timedelta(seconds=30)).timestamp() * 1000)
        end_ts = int((target_dt + timedelta(seconds=20)).timestamp() * 1000)
        target_time_display = target_time_str

    url = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Referer": "https://web3.okx.com/zh-hans/market/dex"
    }

    all_matched_trades = []
    current_id = None
    has_more = "1"
    stop_pagination = False

    if verbose:
        print(f"分析窗口: {target_time_display} (-30s ~ +20s)")
        print("正在努力搬运数据中...")

    session = requests.Session()

    while has_more == "1" and not stop_pagination:
        payload = {
            "desc": True,
            "orderBy": "timestamp",
            "limit": 30,
            "tradingHistoryFilter": {
                "chainId": chain_id,
                "tokenContractAddress": token_address,
                "type": "0",
                "currentUserWalletAddress": "",
                "userAddressList": [],
                "volumeMin": "", "volumeMax": "", 
                "priceMin": "", "priceMax": "", 
                "amountMin": "", "amountMax": ""
            }
        }
        if current_id:
            payload["dataId"] = current_id

        try:
            resp = session.post(f"{url}?t={int(time.time()*1000)}", json=payload, headers=headers, timeout=10)
            resp.raise_for_status()

            res_json = resp.json()
            data_obj = res_json.get("data", {})
            trade_list = data_obj.get("list", [])
            has_more = str(data_obj.get("hasMore", "0"))

            if not trade_list:
                break

            if verbose:
                progress_time = datetime.fromtimestamp(trade_list[-1]['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S')
                print(f"\r>>> 扫描到: {progress_time} (目标截止: {datetime.fromtimestamp(start_ts/1000).strftime('%H:%M:%S')})", end="", flush=True)

            for trade in trade_list:
                ts = int(trade.get('timestamp', 0))
                
                if ts > end_ts:
                    continue 
                if ts < start_ts:
                    stop_pagination = True 
                    break
                
                user_addr = (trade.get("userAddress") or "")
                is_kol = "★ KOL" if user_addr.lower() == kol_address.lower() else ""
                
                all_matched_trades.append({
                    # 你原来的展示列
                    "时间": datetime.fromtimestamp(ts/1000).strftime('%Y-%m-%d %H:%M:%S'),
                    "交易类型": "买入" if trade.get("isBuy") == "1" else "卖出",
                    "USD数量": round(float(trade.get("volume", 0) or 0), 2),
                    "交易者地址": user_addr,
                    "KOL标记": is_kol,

                    # 额外列：落盘/对齐很有用
                    "timestamp_ms": ts,
                    "dataId": trade.get("id"),
                    "token_address": token_address,
                    "kol_address": kol_address,
                    "chain_id": chain_id,
                })

            current_id = trade_list[-1].get("id")

            time.sleep(0.3)
        except Exception as e:
            if verbose:
                print(f"\n请求遇到点麻烦: {e}")
            break

    if all_matched_trades:
        df = pd.DataFrame(all_matched_trades).sort_values(by="timestamp_ms")
        return df
    else:
        return pd.DataFrame()
