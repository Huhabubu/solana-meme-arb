# -*- coding: utf-8 -*-

import requests
import time
import pandas as pd
from datetime import datetime

# --- 新增：安全转换函数 ---
def safe_float(value):
    """
    将数据转换为 float，处理空字符串、None 和转换错误
    """
    if value is None or value == "":
        return 0.0  # 或者返回 None，取决于你后续想要怎么处理缺失值
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0

def safe_int(value):
    """
    将数据转换为 int，处理空字符串、None 和转换错误
    """
    if value is None or value == "":
        return 0
    try:
        # 有些数字可能是字符串 "10.0"，直接转 int 会报错，先转 float 再转 int
        return int(float(value))
    except (ValueError, TypeError):
        return 0


#https://web3.okx.com/priapi/v1/dx/market/v2/tracker/trends/trades/list?chainId=501&timeType=1&tagType=2&rankBy=7&desc=true&t=1766980033048
def get_meme_radar_trends(tag):
    # 1. 基础 URL
    url = "https://web3.okx.com/priapi/v1/dx/market/v2/tracker/trends/trades/list"
    
    tagtype = '2' if tag == 'KOL' else ('1' if tag == 'SMART' else None)
    # 2. 构造动态参数
    params = {
        "chainId": "501",        # Solana
        "timeType": "1",         # 1m 
        "tagType": tagtype,          # 2=KOL
        "rankBy": "7",           # 按流入量排序
        "desc": "true",          # 降序
        "t": int(time.time() * 1000)
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.okx.com/zh-hans/web3/dex/market/meme-radar",
        "Origin": "https://www.okx.com"
    }

    try:
        response = requests.get(url, params=params, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 0:
                raw_list = data.get("data", {}).get("dataList", [])
                
                clean_data = []
                for item in raw_list:
                    # 1. 提取交易详情 (防止 trades 列表为空)
                    trades = item.get('trades', [])
                    trade_detail = trades[0] if trades else {}
                    
                    # 2. 提取标签信息 (防止层级太深报错)
                    tags_list = trade_detail.get('t', [])
                    tags_info = tags_list[0].get('e', {}) if tags_list else {}
                    
                    # 3. 构造中文数据行 (全部使用 safe_float/safe_int)
                    row = {
                        # --- 身份标识 ---
                        '代币符号': item.get('smbl'),
                        '合约地址': item.get('ca'), 
                        
                        # --- 市场状态 (特征) ---
                        '价格(USD)': safe_float(item.get('price')),
                        '市值(USD)': safe_float(item.get('mcap')),
                        '持币人数': safe_int(item.get('holders')), 
                        '24H成交额': safe_float(item.get('vol')),
                        '涨跌幅(%)': safe_float(item.get('change')),
                        
                        # --- 信号强度 ---
                        '净流入(USD)': safe_float(item.get('inflow')),
                        '独立交易人数': safe_int(item.get('uqtrader')),
                        
                        # --- 时间因子 ---
                        # 注意：firstpt 有可能也是 None，需要防护
                        '信号发布时间': datetime.fromtimestamp(safe_float(item.get('firstpt'))/1000).strftime('%Y-%m-%d %H:%M:%S') if item.get('firstpt') else "未知时间",
                        '最新交易时间戳': trade_detail.get('txTime', 0),
                        
                        # --- 聪明钱/KOL 行为分析 ---
                        '买家地址': trade_detail.get('collectAddress'),
                        'KOL名称': tags_info.get('name', 'Unknown'),
                        '买家持仓余额': safe_float(trade_detail.get('balance')), 
                        '买家买入次数': safe_int(trade_detail.get('txsb')),
                        '买家卖出次数': safe_int(trade_detail.get('txss'))
                    }
                    clean_data.append(row)
                
                # 转换为 DataFrame
                df = pd.DataFrame(clean_data)
                return df,data
                
            else:
                print(f"❌ 接口逻辑报错: {data.get('msg')}")
                return pd.DataFrame(),data
        else:
            print(f"❌ 请求失败，状态码: {response.status_code}")
            return pd.DataFrame(),data
            
    except Exception as e:
        # 打印详细的错误行，方便排查
        import traceback
        traceback.print_exc() 
        print(f"⚠️ 发生错误: {e}")
        return pd.DataFrame(),data

# --- 执行与保存 ---
if __name__ == "__main__":
    
    df_result,data = get_meme_radar_trends('KOL')
    
    if not df_result.empty:
        print(">>> 抓取成功，前 5 条数据预览：")
        # 强制显示所有列
        pd.set_option('display.max_columns', None)
        print(df_result[['代币符号', '价格(USD)', '持币人数', 'KOL名称', '买家持仓余额']].head())
    else:
        print(">>> 未获取到数据 (DataFrame为空)")
