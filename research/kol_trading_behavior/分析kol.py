import requests
import time
import pandas as pd

def get_30_earliest_buys_df(wallet_address, chain_id=501, verbose=True):
    # 存放数据：{ token_address: trade_info }
    earliest_buys_dict = {}
    # 存放币种顺序，用来判断是否达到31个
    token_order = []
    
    global_index = None
    block_time_pagination = None
    has_next = True
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://web3.okx.com/zh-hans/market/pnl/wallet-profile",
        "Accept": "application/json"
    }
    
    url = "https://web3.okx.com/priapi/v1/dx/market/v2/pnl/wallet-profile/trade-history"
    
    if verbose:
        print(f"开始抓取地址 [{wallet_address}]，目标：找齐 31 个不同币种后截断...")

    session = requests.Session()
    stop_loop = False

    while has_next and not stop_loop:
        params = {
            "walletAddress": wallet_address,
            "chainId": chain_id,
            "pageSize": 50,
            "tradeType": "1,2",
            "filterRisk": "true",
            "t": int(time.time() * 1000)
        }
        if global_index and block_time_pagination:
            params.update({"globalIndex": global_index, "blockTimePagination": block_time_pagination})
            
        try:
            response = session.get(url, params=params, headers=headers, timeout=10)
            response.raise_for_status()

            data = response.json().get("data", {})
            rows = data.get("rows", [])
            has_next = data.get("hasNext", False)
            
            if not rows:
                break

            for trade in rows:
                token_addr = trade.get("tokenContractAddress")
                if not token_addr:
                    continue

                # 只处理买入记录
                if trade.get("type") == 1:
                    amount = float(trade.get("amount", 0) or 0)
                    price = float(trade.get("price", 0) or 0)
                    block_time_ms = int(trade.get("blockTime") or 0)

                    trade_info = {
                        "kol_address": wallet_address,
                        "symbol": trade.get("tokenSymbol"),
                        "address": token_addr,
                        "amount": amount,
                        "price": price,
                        "total_value_usd": amount * price,
                        "time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(block_time_ms/1000)),
                        "blockTime_ms": block_time_ms,
                        "txHash": trade.get("txHash")
                    }

                    if token_addr not in earliest_buys_dict:
                        token_order.append(token_addr)
                        earliest_buys_dict[token_addr] = trade_info
                        
                        # 找齐 31 个就停止
                        if len(token_order) == 31:
                            if verbose:
                                print("已识别到第 31 个币种，正在停止并截断...")
                            stop_loop = True
                            break
                    else:
                        # 只在更早时覆盖（更稳）
                        old_bt = int(earliest_buys_dict[token_addr].get("blockTime_ms") or 0)
                        if old_bt == 0 or (block_time_ms != 0 and block_time_ms < old_bt):
                            earliest_buys_dict[token_addr] = trade_info

            if not stop_loop:
                last_row = rows[-1]
                global_index = last_row.get("globalIndex")
                block_time_pagination = last_row.get("blockTime")
                if verbose:
                    print(f"当前已找到 {len(token_order)} 个币种，继续翻页...")
            
        except Exception as e:
            if verbose:
                print(f"请求异常: {e}")
            break

    # 丢掉第 31 个币，只保留前 30 个
    final_tokens = token_order[:30]
    final_results = [earliest_buys_dict[addr] for addr in final_tokens if addr in earliest_buys_dict]

    df = pd.DataFrame(final_results)
    if df.empty:
        return df

    # 列顺序（你要的那几列在前面；额外字段保留便于后续）
    show_cols = ['kol_address', 'symbol', 'time', 'total_value_usd', 'amount', 'price', 'address', 'blockTime_ms', 'txHash']
    cols = [c for c in show_cols if c in df.columns] + [c for c in df.columns if c not in show_cols]
    df = df[cols]

    return df
