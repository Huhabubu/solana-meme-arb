#https://web3.okx.com/priapi/v5/dex/token/market/dex-token-hlc-candles?chainId=501&address=6REjfDLp9f2iG9G1bbBMtRstJ4nHix5kwQUjxmY1CYEE&after=1766198782000&bar=1m&limit=1000&t=1766198716371

import requests
import time
import pandas as pd

def get_okx_hlc_candles(token_address,after,bar='1s', limit=1440):
    """
    获取 OKX Web3 令牌 HLC K线数据 (修正版)
    """
    t = int(time.time() * 1000)
    url = "https://web3.okx.com/priapi/v5/dex/token/market/dex-token-hlc-candles"
    
    params = {
        "chainId": "501",
        "address": token_address,
        "after": after, 
        "bar": bar,
        "limit": limit,
        "t": t
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"https://www.okx.com/zh-hans/web3/dex/market/token/501/{token_address}",
        "Origin": "https://www.okx.com"
    }

    try:
        response = requests.get(url, params=params, headers=headers)
        if response.status_code == 200:
            result = response.json()
            if result.get("code") == "0":
                candles = result.get("data", [])
                
                if not candles:
                    print("⚠️ 未获取到 K线数据。")
                    return None

                # ⭐ 核心修正点：定义全部 8 个列名
                columns = [
                    'timestamp', 'open', 'high', 'low', 
                    'close', 'volume', 'volumeUsd', 'confirm'
                ]
                
                # 转换为 DataFrame
                df = pd.DataFrame(candles, columns=columns)
                
                # 数据类型转换 (将字符串转换为数值，否则无法计算)
                num_cols = ['open', 'high', 'low', 'close', 'volume', 'volumeUsd']
                df[num_cols] = df[num_cols].apply(pd.to_numeric)
                
                # 时间戳转换
                df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
                
                # 按照你的需求，如果只想保留 HLC，可以在此处过滤
                # df = df[['timestamp', 'high', 'low', 'close']]

                print(f"📊 成功获取 {len(df)} 根 K线数据")
                print(df.tail(3)) # 打印最后三行
                
                return df
            else:
                print(f"❌ 接口逻辑错误: {result}")
        else:
            print(f"❌ 请求失败, 状态码: {response.status_code}")

    except Exception as e:
        print(f"⚠️ 发生错误: {e}")

if __name__ == "__main__":
    TOKEN = "3oEHLDg8VokqBM37u7ohkbKUKcdPE8sT8Btgrxovpump"
    df_candles = get_okx_hlc_candles(TOKEN)
