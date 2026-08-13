use anyhow::{bail, Context, Result};
use reqwest::Client;
use serde::Deserialize;
use serde_json::json;

use crate::model::PoolInfo;

pub const PUBLIC_MAINNET_RPC: &str = "https://api.mainnet-beta.solana.com";

#[derive(Debug, Deserialize)]
struct RpcEnvelope {
    result: Option<RpcResult>,
    error: Option<RpcError>,
}

#[derive(Debug, Deserialize)]
struct RpcResult {
    value: Vec<Option<RpcAccount>>,
}

#[derive(Debug, Deserialize)]
struct RpcAccount {
    owner: String,
}

#[derive(Debug, Deserialize)]
struct RpcError {
    code: i64,
    message: String,
}

/// 只读取账户 owner，因此通过 dataSlice 请求 0 字节账户数据，减少 RPC 响应体积。
pub async fn fetch_account_owners(
    client: &Client,
    rpc_url: &str,
    addresses: &[String],
) -> Result<Vec<Option<String>>> {
    if addresses.is_empty() {
        return Ok(Vec::new());
    }
    if addresses.len() > 100 {
        bail!("getMultipleAccounts supports at most 100 addresses per request");
    }

    let request = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getMultipleAccounts",
        "params": [
            addresses,
            {
                "commitment": "finalized",
                "encoding": "base64",
                "dataSlice": {"offset": 0, "length": 0}
            }
        ]
    });

    let body = client
        .post(rpc_url)
        .json(&request)
        .send()
        .await?
        .error_for_status()?
        .text()
        .await?;

    parse_account_owners(&body)
}

fn parse_account_owners(body: &str) -> Result<Vec<Option<String>>> {
    let envelope: RpcEnvelope = serde_json::from_str(body).context("invalid Solana RPC JSON")?;
    if let Some(error) = envelope.error {
        bail!("Solana RPC error {}: {}", error.code, error.message);
    }

    let result = envelope.result.context("Solana RPC response missing result")?;
    Ok(result
        .value
        .into_iter()
        .map(|account| account.map(|account| account.owner))
        .collect())
}

/// 校验候选池在链上真实存在，并且账户 owner 与预期 DEX Program 一致。
pub fn verify_pool_accounts(pools: &[PoolInfo], owners: &[Option<String>]) -> Result<()> {
    if pools.len() != owners.len() {
        bail!(
            "pool/account result length mismatch: {} pools, {} owners",
            pools.len(),
            owners.len()
        );
    }

    for (pool, owner) in pools.iter().zip(owners) {
        let actual_owner = owner
            .as_deref()
            .with_context(|| format!("pool account does not exist: {}", pool.address))?;
        let expected_owner = pool
            .program_id
            .as_deref()
            .with_context(|| format!("missing expected program id for pool: {}", pool.address))?;

        if actual_owner != expected_owner {
            bail!(
                "pool owner mismatch for {}: expected {}, got {}",
                pool.address,
                expected_owner,
                actual_owner
            );
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Dex;

    fn pool(address: &str, program_id: Option<&str>) -> PoolInfo {
        PoolInfo {
            dex: Dex::Raydium,
            address: address.into(),
            pool_type: "test".into(),
            program_id: program_id.map(str::to_owned),
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd: 1_000.0,
        }
    }

    #[test]
    fn parses_rpc_owners_and_missing_accounts_in_order() {
        let body = r#"{
            "jsonrpc":"2.0",
            "result":{"context":{"slot":1},"value":[{"owner":"program-a"},null,{"owner":"program-c"}]},
            "id":1
        }"#;

        let owners = parse_account_owners(body).unwrap();
        assert_eq!(
            owners,
            vec![Some("program-a".into()), None, Some("program-c".into())]
        );
    }

    #[test]
    fn rejects_rpc_error() {
        let body = r#"{"jsonrpc":"2.0","error":{"code":-32602,"message":"bad params"},"id":1}"#;
        assert!(parse_account_owners(body).is_err());
    }

    #[test]
    fn verifies_matching_account_owner() {
        let pools = vec![pool("pool-a", Some("program-a"))];
        let owners = vec![Some("program-a".into())];
        assert!(verify_pool_accounts(&pools, &owners).is_ok());
    }

    #[test]
    fn rejects_missing_account() {
        let pools = vec![pool("pool-a", Some("program-a"))];
        assert!(verify_pool_accounts(&pools, &[None]).is_err());
    }

    #[test]
    fn rejects_owner_mismatch() {
        let pools = vec![pool("pool-a", Some("program-a"))];
        let owners = vec![Some("wrong-program".into())];
        assert!(verify_pool_accounts(&pools, &owners).is_err());
    }

    #[test]
    fn rejects_missing_expected_program_id() {
        let pools = vec![pool("pool-a", None)];
        let owners = vec![Some("program-a".into())];
        assert!(verify_pool_accounts(&pools, &owners).is_err());
    }

    #[test]
    fn rejects_result_length_mismatch() {
        let pools = vec![pool("pool-a", Some("program-a"))];
        assert!(verify_pool_accounts(&pools, &[]).is_err());
    }
}
