use anyhow::{Context, Result};
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;

use crate::model::{Dex, PoolInfo};
use crate::serde_utils::number_from_value;

const DLMM_URL: &str = "https://dlmm.datapi.meteora.ag/pools";
const DAMM_V2_URL: &str = "https://damm-v2.datapi.meteora.ag/pools";

#[derive(Debug, Deserialize)]
struct Response {
    data: Vec<RawPool>,
}

#[derive(Debug, Deserialize)]
struct RawPool {
    address: String,
    name: String,
    #[serde(default)]
    is_blacklisted: bool,
    token_x: RawToken,
    token_y: RawToken,
    tvl: Value,
}

#[derive(Debug, Deserialize)]
struct RawToken {
    address: String,
}

pub async fn fetch_dlmm_pools(
    client: &Client,
    mint_x: &str,
    mint_y: &str,
) -> Result<Vec<PoolInfo>> {
    fetch_pools(client, DLMM_URL, Dex::MeteoraDlmm, "DLMM", mint_x, mint_y).await
}

pub async fn fetch_damm_v2_pools(
    client: &Client,
    mint_x: &str,
    mint_y: &str,
) -> Result<Vec<PoolInfo>> {
    fetch_pools(
        client,
        DAMM_V2_URL,
        Dex::MeteoraDammV2,
        "DAMM v2",
        mint_x,
        mint_y,
    )
    .await
}

async fn fetch_pools(
    client: &Client,
    url: &str,
    dex: Dex,
    pool_type: &str,
    mint_x: &str,
    mint_y: &str,
) -> Result<Vec<PoolInfo>> {
    // Meteora 的 query 可以按名称、token 或地址搜索；随后仍在本地做精确 mint pair 校验。
    let body = client
        .get(url)
        .query(&[
            ("page", "1"),
            ("page_size", "1000"),
            ("query", mint_x),
            ("sort_by", "tvl:desc"),
            ("filter_by", "is_blacklisted=false"),
        ])
        .send()
        .await?
        .error_for_status()?
        .text()
        .await?;

    parse_response(&body, dex, pool_type, mint_x, mint_y)
}

fn parse_response(
    body: &str,
    dex: Dex,
    pool_type: &str,
    mint_x: &str,
    mint_y: &str,
) -> Result<Vec<PoolInfo>> {
    let response: Response = serde_json::from_str(body).context("invalid Meteora JSON")?;

    response
        .data
        .into_iter()
        .filter(|pool| !pool.is_blacklisted)
        .map(|pool| {
            Ok(PoolInfo {
                dex,
                address: pool.address,
                pool_type: format!("{pool_type}: {}", pool.name),
                program_id: None,
                mint_a: pool.token_x.address,
                mint_b: pool.token_y.address,
                tvl_usd: number_from_value(&pool.tvl)?,
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
    fn parses_exact_pair_and_excludes_blacklisted_pool() {
        let body = r#"{
            "data": [
                {"address":"pool-1","name":"A-B","is_blacklisted":false,"token_x":{"address":"MintA"},"token_y":{"address":"MintB"},"tvl":9000},
                {"address":"pool-2","name":"A-B","is_blacklisted":true,"token_x":{"address":"MintA"},"token_y":{"address":"MintB"},"tvl":10000},
                {"address":"pool-3","name":"A-C","is_blacklisted":false,"token_x":{"address":"MintA"},"token_y":{"address":"Other"},"tvl":8000}
            ]
        }"#;

        let pools = parse_response(body, Dex::MeteoraDlmm, "DLMM", A, B).unwrap();
        assert_eq!(pools.len(), 1);
        assert_eq!(pools[0].address, "pool-1");
        assert_eq!(pools[0].tvl_usd, 9000.0);
        assert!(pools[0].pool_type.starts_with("DLMM:"));
    }

    #[test]
    fn accepts_numeric_string_tvl() {
        let body = r#"{"data":[{"address":"pool","name":"A-B","token_x":{"address":"MintB"},"token_y":{"address":"MintA"},"tvl":"42.5"}]}"#;
        let pools = parse_response(body, Dex::MeteoraDammV2, "DAMM v2", A, B).unwrap();
        assert_eq!(pools[0].tvl_usd, 42.5);
    }
}
