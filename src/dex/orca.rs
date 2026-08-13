use anyhow::{Context, Result};
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;

use crate::model::{Dex, PoolInfo};
use crate::serde_utils::number_from_value;

const BASE_URL: &str = "https://api.orca.so/v2/solana/pools";
const WHIRLPOOL_PROGRAM_ID: &str = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc";

#[derive(Debug, Deserialize)]
struct Response {
    data: Vec<RawPool>,
}

#[derive(Debug, Deserialize)]
struct RawPool {
    address: String,
    #[serde(rename = "tokenMintA")]
    token_mint_a: String,
    #[serde(rename = "tokenMintB")]
    token_mint_b: String,
    #[serde(rename = "tvlUsdc")]
    tvl_usdc: Value,
    #[serde(rename = "poolType")]
    pool_type: String,
}

pub async fn fetch_pools(client: &Client, mint_x: &str, mint_y: &str) -> Result<Vec<PoolInfo>> {
    let pair = format!("{mint_x},{mint_y}");
    let body = client
        .get(BASE_URL)
        .query(&[
            ("tokensBothOf", pair.as_str()),
            ("sortBy", "tvl"),
            ("sortDirection", "desc"),
            ("size", "20"),
            ("stats", "24h"),
        ])
        .send()
        .await?
        .error_for_status()?
        .text()
        .await?;

    parse_response(&body, mint_x, mint_y)
}

fn parse_response(body: &str, mint_x: &str, mint_y: &str) -> Result<Vec<PoolInfo>> {
    let response: Response = serde_json::from_str(body).context("invalid Orca JSON")?;

    response
        .data
        .into_iter()
        .map(|pool| {
            Ok(PoolInfo {
                dex: Dex::Orca,
                address: pool.address,
                pool_type: pool.pool_type,
                program_id: Some(WHIRLPOOL_PROGRAM_ID.into()),
                mint_a: pool.token_mint_a,
                mint_b: pool.token_mint_b,
                tvl_usd: number_from_value(&pool.tvl_usdc)?,
            })
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
    fn parses_string_tvl_exact_pair_and_program_id() {
        let body = r#"{
            "data": [
                {"address":"pool-1","tokenMintA":"MintB","tokenMintB":"MintA","tvlUsdc":"5000.25","poolType":"whirlpool"},
                {"address":"pool-2","tokenMintA":"MintA","tokenMintB":"Other","tvlUsdc":"10","poolType":"whirlpool"}
            ]
        }"#;

        let pools = parse_response(body, A, B).unwrap();
        assert_eq!(pools.len(), 1);
        assert_eq!(pools[0].address, "pool-1");
        assert_eq!(pools[0].tvl_usd, 5000.25);
        assert_eq!(pools[0].dex, Dex::Orca);
        assert_eq!(pools[0].program_id.as_deref(), Some(WHIRLPOOL_PROGRAM_ID));
    }

    #[test]
    fn rejects_invalid_tvl() {
        let body = r#"{"data":[{"address":"pool","tokenMintA":"MintA","tokenMintB":"MintB","tvlUsdc":"bad","poolType":"whirlpool"}]}"#;
        assert!(parse_response(body, A, B).is_err());
    }
}
