use anyhow::{bail, Context, Result};
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;

use crate::{
    model::{Dex, PoolInfo},
    serde_utils::number_from_value,
};

const BASE_URL: &str = "https://api-v3.raydium.io/pools/info/mint";

#[derive(Debug, Deserialize)]
struct Envelope {
    success: bool,
    msg: Option<String>,
    data: Option<Page>,
}

#[derive(Debug, Deserialize)]
struct Page {
    data: Vec<RawPool>,
}

#[derive(Debug, Deserialize)]
struct RawPool {
    id: String,
    #[serde(rename = "type")]
    pool_type: String,
    #[serde(rename = "programId")]
    program_id: String,
    mint1: RawMint,
    mint2: RawMint,
    tvl: Value,
}

#[derive(Debug, Deserialize)]
struct RawMint {
    address: String,
}

pub async fn fetch_pools(client: &Client, mint_x: &str, mint_y: &str) -> Result<Vec<PoolInfo>> {
    let body = client
        .get(BASE_URL)
        .query(&[
            ("mint1", mint_x),
            ("mint2", mint_y),
            ("poolType", "all"),
            ("poolSortField", "liquidity"),
            ("sortType", "desc"),
            ("pageSize", "20"),
            ("page", "1"),
        ])
        .send()
        .await?
        .error_for_status()?
        .text()
        .await?;

    parse_response(&body, mint_x, mint_y)
}

fn parse_response(body: &str, mint_x: &str, mint_y: &str) -> Result<Vec<PoolInfo>> {
    let envelope: Envelope = serde_json::from_str(body).context("invalid Raydium JSON")?;
    if !envelope.success {
        bail!(
            "Raydium API error: {}",
            envelope.msg.unwrap_or_else(|| "unknown error".into())
        );
    }

    let page = envelope.data.context("Raydium response missing data")?;
    page.data
        .into_iter()
        .map(|pool| {
            let info = PoolInfo {
                dex: Dex::Raydium,
                address: pool.id,
                pool_type: pool.pool_type,
                program_id: Some(pool.program_id),
                mint_a: pool.mint1.address,
                mint_b: pool.mint2.address,
                tvl_usd: number_from_value(&pool.tvl)?,
            };
            Ok(info)
        })
        .collect::<Result<Vec<_>>>()
        .map(|pools| {
            pools
                .into_iter()
                .filter(|pool| pool.matches_pair(mint_x, mint_y))
                .collect()
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    const A: &str = "MintA";
    const B: &str = "MintB";

    #[test]
    fn parses_valid_pool_and_filters_other_pairs() {
        let body = r#"{
            "success": true,
            "data": {"data": [
                {"id":"pool-1","type":"Concentrated","programId":"program-1","mint1":{"address":"MintA"},"mint2":{"address":"MintB"},"tvl":1234.5},
                {"id":"pool-2","type":"Standard","programId":"program-2","mint1":{"address":"MintA"},"mint2":{"address":"Other"},"tvl":10}
            ]}
        }"#;

        let pools = parse_response(body, A, B).unwrap();
        assert_eq!(pools.len(), 1);
        assert_eq!(pools[0].address, "pool-1");
        assert_eq!(pools[0].program_id.as_deref(), Some("program-1"));
        assert_eq!(pools[0].tvl_usd, 1234.5);
    }

    #[test]
    fn rejects_api_error_envelope() {
        let body = r#"{"success":false,"msg":"bad request"}"#;
        assert!(parse_response(body, A, B).is_err());
    }
}
