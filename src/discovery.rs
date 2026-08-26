use std::cmp::Ordering;

use anyhow::Result;
use reqwest::Client;

use crate::dex::{meteora, orca, raydium};
use crate::model::PoolInfo;

pub const MIN_MONITOR_TVL_USD: f64 = 1_000.0;
pub const MAX_POOLS_PER_DEX: usize = 3;

pub async fn discover_pair(client: &Client, mint_x: &str, mint_y: &str) -> Result<Vec<PoolInfo>> {
    let (raydium, orca, meteora_dlmm, meteora_damm_v2) = tokio::try_join!(
        raydium::fetch_pools(client, mint_x, mint_y),
        orca::fetch_pools(client, mint_x, mint_y),
        meteora::fetch_dlmm_pools(client, mint_x, mint_y),
        meteora::fetch_damm_v2_pools(client, mint_x, mint_y),
    )?;

    let mut pools = Vec::new();
    pools.extend(raydium);
    pools.extend(orca);
    pools.extend(meteora_dlmm);
    pools.extend(meteora_damm_v2);
    sort_by_tvl_desc(&mut pools);
    Ok(pools)
}

pub async fn discover_quote_pair(
    client: &Client,
    mint_x: &str,
    mint_y: &str,
) -> Result<Vec<PoolInfo>> {
    let (raydium, orca, meteora_dlmm) = tokio::try_join!(
        raydium::fetch_pools(client, mint_x, mint_y),
        orca::fetch_pools(client, mint_x, mint_y),
        meteora::fetch_dlmm_pools(client, mint_x, mint_y),
    )?;

    let mut pools = Vec::new();
    pools.extend(raydium);
    pools.extend(orca);
    pools.extend(meteora_dlmm);
    Ok(pools)
}

/// V0/V1 只保留有基本流动性的少量深池，避免订阅大量灰尘池浪费 RPC/WSS 配额。
/// 传入的数据不要求预排序；返回结果始终按 TVL 从高到低排列。
pub fn select_monitoring_candidates(
    pools: &[PoolInfo],
    min_tvl_usd: f64,
    max_per_dex: usize,
) -> Vec<PoolInfo> {
    let mut sorted = pools.to_vec();
    sort_by_tvl_desc(&mut sorted);

    let mut selected = Vec::new();
    for pool in sorted {
        if pool.tvl_usd < min_tvl_usd {
            continue;
        }

        let same_dex_count = selected
            .iter()
            .filter(|selected_pool: &&PoolInfo| selected_pool.dex == pool.dex)
            .count();
        if same_dex_count < max_per_dex {
            selected.push(pool);
        }
    }

    selected
}

fn sort_by_tvl_desc(pools: &mut [PoolInfo]) {
    pools.sort_by(|left, right| {
        right
            .tvl_usd
            .partial_cmp(&left.tvl_usd)
            .unwrap_or(Ordering::Equal)
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Dex;

    fn pool(dex: Dex, address: &str, tvl_usd: f64) -> PoolInfo {
        PoolInfo {
            dex,
            address: address.into(),
            pool_type: "test".into(),
            program_id: None,
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd,
        }
    }

    #[test]
    fn sorts_pools_by_tvl_descending() {
        let mut pools = vec![
            pool(Dex::Raydium, "small", 1.0),
            pool(Dex::Raydium, "large", 100.0),
            pool(Dex::Raydium, "mid", 10.0),
        ];
        sort_by_tvl_desc(&mut pools);
        let addresses: Vec<_> = pools.iter().map(|pool| pool.address.as_str()).collect();
        assert_eq!(addresses, vec!["large", "mid", "small"]);
    }

    #[test]
    fn candidate_selection_filters_low_tvl_and_limits_each_dex() {
        let pools = vec![
            pool(Dex::Raydium, "r3", 3_000.0),
            pool(Dex::Raydium, "r1", 10_000.0),
            pool(Dex::Raydium, "r2", 5_000.0),
            pool(Dex::Raydium, "r4", 2_000.0),
            pool(Dex::Orca, "o1", 8_000.0),
            pool(Dex::Orca, "o2", 900.0),
        ];

        let selected = select_monitoring_candidates(&pools, 1_000.0, 3);
        let addresses: Vec<_> = selected.iter().map(|pool| pool.address.as_str()).collect();
        assert_eq!(addresses, vec!["r1", "o1", "r2", "r3"]);
    }

    #[test]
    fn candidate_selection_with_zero_limit_returns_empty() {
        let pools = vec![pool(Dex::Orca, "o1", 8_000.0)];
        assert!(select_monitoring_candidates(&pools, 1_000.0, 0).is_empty());
    }
}
