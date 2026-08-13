use std::cmp::Ordering;

use anyhow::Result;
use reqwest::Client;

use crate::dex::{meteora, orca, raydium};
use crate::model::PoolInfo;

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

    fn pool(address: &str, tvl_usd: f64) -> PoolInfo {
        PoolInfo {
            dex: Dex::Raydium,
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
        let mut pools = vec![pool("small", 1.0), pool("large", 100.0), pool("mid", 10.0)];
        sort_by_tvl_desc(&mut pools);
        let addresses: Vec<_> = pools.iter().map(|pool| pool.address.as_str()).collect();
        assert_eq!(addresses, vec!["large", "mid", "small"]);
    }
}
