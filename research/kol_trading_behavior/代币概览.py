# -*- coding: utf-8 -*-
import requests
import time
import pandas as pd
from datetime import datetime

# 严格定义缺失值为 None
MISSING_VALUE = None

def safe_parse(value, target_type=float):
    """
    严谨的数据清洗函数：
    1. 区分 '0' / 0 和 '' (空值)。
    2. 只有在真正缺失（None 或 空字符串）时返回 MISSING_VALUE。
    """
    if value is None or str(value).strip() == "" or str(value).lower() == "nan":
        return MISSING_VALUE
    try:
        # 如果目标是 int，先转 float 再转 int 防止 '1.0' 报错
        if target_type == int:
            return int(float(value))
        return target_type(value)
    except (ValueError, TypeError):
        return MISSING_VALUE

def get_tag_value(tag_vo, key, target_type=float):
    """从 tokenTagVO 复杂的嵌套结构中提取并解析 tagValue"""
    item = tag_vo.get(key, {})
    if not item:
        return MISSING_VALUE
    return safe_parse(item.get('tagValue'), target_type)

def get_okx_token_details(token_address, chain_id="501"):
    timestamp = int(time.time() * 1000)
    url = f"https://web3.okx.com/priapi/v1/dx/market/v2/token/overview"
    params = {
        "chainId": chain_id,
        "tokenContractAddress": token_address,
        "t": timestamp
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        response = requests.get(url, params=params, headers=headers)
        if response.status_code != 200:
            return pd.DataFrame()
            
        result = response.json()
        data = result.get("data", {})
        
        # 提取模块
        basic = data.get("basicInfo", {})
        market = data.get("marketInfo", {})
        meme = data.get("memeInfo", {})
        tags = data.get("tokenTagVO", {})
        social = data.get("socialMedia", {})
        third_party = data.get("tokenThirdPartInfo", {})

        # --- 特征提取开始 (全字段覆盖) ---
        feature_row = {
            # 1. 基础信息
            '代币名称': basic.get('tokenName', MISSING_VALUE),
            '代币符号': basic.get('tokenSymbol', MISSING_VALUE),
            '合约地址': token_address,
            '所属链': basic.get('chainName', MISSING_VALUE),
            '创建时间': datetime.fromtimestamp(int(safe_parse(market.get('tokenCreateTime'), int))/1000).strftime('%Y-%m-%d %H:%M:%S') if market.get('tokenCreateTime') else MISSING_VALUE,
            '是否Meme': safe_parse(basic.get('isMeme'), int),
            '是否原生代币': basic.get('isNativeToken'), # 通常是 "false" 字符串
            
            # 2. 价格与涨跌幅
            '市值(MCap)': safe_parse(market.get('marketCap')),
            '全稀释估值(FDV)': safe_parse(market.get('fdv')),
            '5M涨跌幅(%)': safe_parse(market.get('priceChange5M')),
            '1H涨跌幅(%)': safe_parse(market.get('priceChange1H')),
            '4H涨跌幅(%)': safe_parse(market.get('priceChange4H')),
            '24H涨跌幅(%)': safe_parse(market.get('priceChange24H')),
            
            # 3. 流动性与供应
            '总流动性(Liquidity)': safe_parse(market.get('totalLiquidity')),
            '流通供应量': safe_parse(market.get('circulatingSupply')),
            '最大供应量': safe_parse(market.get('maxSupply')),
            '持币人数': safe_parse(market.get('holders'), int),
            'LP燃烧比例(%)': safe_parse(market.get('lpTokenBurntRatio')),
            
            # 4. 开发者风险画像 (关键指标)
            '开发者地址': basic.get('tokenCreatorAddress', MISSING_VALUE),
            'Dev_发币总数': safe_parse(basic.get('devCreateTokenCount'), int),
            'Dev_跑路次数': safe_parse(basic.get('devRugPullTokenCount'), int),
            'Dev_成功发射数': safe_parse(basic.get('launchedTokenCount'), int),
            'Dev_持币比例(%)': get_tag_value(tags, 'devHoldingRatio'),
            'Dev_持仓状态': tags.get('devHoldingStatus', {}).get('tagValue', MISSING_VALUE), # 如 sellAll
            '创建者历史铸造次数': get_tag_value(tags, 'creatorMintCnt', int),
            '创建者迁移次数': get_tag_value(tags, 'creatorMigrationCnt', int),

            # 5. 筹码分布与阴谋集团分析 (Bundle/Sniper)
            '捆绑包_持仓比例(%)': get_tag_value(tags, 'bundleHoldingRatio'),
            '捆绑包_地址数': get_tag_value(tags, 'bundleHolderAmount', int),
            '捆绑包_代币持有量': get_tag_value(tags, 'bundleHoldingTokenAmount'),
            '捆绑包_历史最高比例(%)': get_tag_value(tags, 'bundleHoldingRatioAth'),
            '狙击手_持币比例(%)': get_tag_value(tags, 'sniperHoldingRatio'),
            '狙击手_总数': safe_parse(market.get('snipersTotal'), int),
            '狙击手_已清仓数': safe_parse(market.get('snipersClear'), int),
            '新钱包_持币比例(%)': get_tag_value(tags, 'freshHoldingRatio'),
            '可疑持仓比例(%)': safe_parse(market.get('suspiciousRatio')),
            '疑似钓鱼钱包持仓比例(%)': get_tag_value(tags, 'suspectedPhishingWalletHoldingRatio'),
            '聪明钱_持仓状态': tags.get('smartMoneyHoldingStatus', {}).get('tagValue', MISSING_VALUE),
            
            # 6. 安全与风控
            '风险等级': safe_parse(market.get('riskLevel'), int),
            '风控分级': safe_parse(market.get('riskControlLevel'), int),
            
            # 7. Pump.fun 特有数据
            'Pump进度(%)': safe_parse(meme.get('progress')) * 100 if meme.get('progress') else MISSING_VALUE,
            'Pump交易总笔数': safe_parse(meme.get('transactions'), int),
            'Pump总成交量': safe_parse(meme.get('volume')),
            
            # 8. 社交与第三方
            '推特链接': social.get('twitter', MISSING_VALUE),
            '是否有推特': 1 if social.get('twitter') else 0,
            '是否有电报': 1 if social.get('telegram') else 0,
            '综合热度评分': safe_parse(social.get('score')),
            'BubbleMaps链接': third_party.get('bubbleMapsUrl', MISSING_VALUE)
        }

        return pd.DataFrame([feature_row])

    except Exception as e:
        print(f"⚠️ 解析异常: {e}")
        return pd.DataFrame()

if __name__ == "__main__":
    target_token = "DjbpJ9WMj8vSj5JFMtkqVY5i748L42XmFoUXVyJNpump"
    df = get_okx_token_details(target_token)
    
    if not df.empty:
        print(">>> 扩充后的代币特征数据 (全字段预览):")
        # 转置显示方便查看每一个中文字段是否正确对齐
        print(df.T)
        
        # 示例：检查某个空字段是否为 None 而非 0
        lp_burn = df.iloc[0]['LP燃烧比例(%)']
        print(f"\n检查缺失值处理 - LP燃烧比例: {lp_burn} (类型: {type(lp_burn)})")
