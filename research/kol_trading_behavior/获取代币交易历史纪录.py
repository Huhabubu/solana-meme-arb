import requests
import time
import pandas as pd

def fetch_trading_df(max_pages=30):
    url = "https://web3.okx.com/priapi/v1/dx/market/v2/trading-history/filter-list?t=1769310180289"
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    payload = {
        "desc": True,
        "orderBy": "timestamp",
        "limit": 30,
        "tradingHistoryFilter": {
            "chainId": "56",
            "tokenContractAddress": "0x845eda2d49853b41ccfe65b47defc83e9f164444",
            "type": "0",
            "currentUserWalletAddress": "0xc4ceea6c149a195d2d212d9481aa98a16864149a",
            "userAddressList": [],
            "volumeMin": "", "volumeMax": "",
            "priceMin": "", "priceMax": "",
            "amountMin": "", "amountMax": ""
        }
    }

    all_data = []
    page_count = 0
    
    while page_count < max_pages:
        try:
            print(f"正在获取第 {page_count + 1} 页数据...")
            response = requests.post(url, headers=headers, json=payload)
            res_json = response.json()
            
            if res_json.get("code") != 0:
                break

            items = res_json.get("data", {}).get("list", [])
            if not items:
                break

            for item in items:
                # 解析字段
                row = {
                    "秒级时间": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(item['timestamp'] / 1000)),
                    "交易地址": item.get("userAddress"),
                    "用户标签": str(item.get("tagList", [])), # 列表转字符串防止报错
                    "交易类型": "买入" if item.get("isBuy") == "1" else "卖出",
                    "总价值(usd)": item.get("volume"),
                    "价格(土狗币价格)": item.get("price"),
                    "mev标识": "是" if item.get("mevFlag") == 1 else "否",
                    "该用户总交易次数": item.get("tradeTotalCount")
                }
                all_data.append(row)

            # 翻页逻辑
            if res_json["data"].get("hasMore") == "1":
                payload["dataId"] = items[-1]["id"]
                page_count += 1
                time.sleep(1) # 频率控制
            else:
                break
                
        except Exception as e:
            print(f"爬取中断: {e}")
            break

    # 转换为 DataFrame
    df = pd.DataFrame(all_data)
    return df

# 执行并查看结果
df_result = fetch_trading_df(max_pages=3)
print(df_result.head())
