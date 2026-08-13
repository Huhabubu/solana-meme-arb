use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::model::PoolInfo;

pub const PUBLIC_MAINNET_RPC: &str = "https://api.mainnet-beta.solana.com";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountData {
    pub owner: String,
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountBatch {
    pub slot: u64,
    pub accounts: Vec<Option<AccountData>>,
}

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
struct FullRpcEnvelope {
    result: Option<FullRpcResult>,
    error: Option<RpcError>,
}

#[derive(Debug, Deserialize)]
struct FullRpcResult {
    context: RpcContext,
    value: Vec<Option<FullRpcAccount>>,
}

#[derive(Debug, Deserialize)]
struct RpcContext {
    slot: u64,
}

#[derive(Debug, Deserialize)]
struct FullRpcAccount {
    owner: String,
    data: (String, String),
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

/// 读取完整账户字节。错误信息刻意不携带 rpc_url，避免 Helius API Key 被 reqwest 错误链打印。
pub async fn fetch_accounts(
    client: &Client,
    rpc_url: &str,
    addresses: &[String],
    min_context_slot: Option<u64>,
) -> Result<AccountBatch> {
    if addresses.is_empty() {
        return Ok(AccountBatch {
            slot: min_context_slot.unwrap_or_default(),
            accounts: Vec::new(),
        });
    }

    let request = build_full_accounts_request(addresses, min_context_slot)?;
    let response = client
        .post(rpc_url)
        .json(&request)
        .send()
        .await
        .map_err(|_| anyhow::anyhow!("Solana RPC full-account request failed"))?;
    let status = response.status();
    if !status.is_success() {
        bail!("Solana RPC full-account request returned status {status}");
    }
    let body = response
        .text()
        .await
        .map_err(|_| anyhow::anyhow!("failed to read Solana RPC full-account response"))?;

    parse_full_accounts_response(&body)
}

fn build_full_accounts_request(addresses: &[String], min_context_slot: Option<u64>) -> Result<Value> {
    if addresses.len() > 100 {
        bail!("getMultipleAccounts supports at most 100 addresses per request");
    }

    let mut config = json!({
        "commitment": "confirmed",
        "encoding": "base64"
    });
    if let Some(slot) = min_context_slot {
        config["minContextSlot"] = json!(slot);
    }

    Ok(json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getMultipleAccounts",
        "params": [addresses, config]
    }))
}

fn parse_account_owners(body: &str) -> Result<Vec<Option<String>>> {
    let envelope: RpcEnvelope = serde_json::from_str(body).context("invalid Solana RPC JSON")?;
    if let Some(error) = envelope.error {
        bail!("Solana RPC error {}: {}", error.code, error.message);
    }

    let result = envelope
        .result
        .context("Solana RPC response missing result")?;
    Ok(result
        .value
        .into_iter()
        .map(|account| account.map(|account| account.owner))
        .collect())
}

fn parse_full_accounts_response(body: &str) -> Result<AccountBatch> {
    let envelope: FullRpcEnvelope =
        serde_json::from_str(body).context("invalid Solana full-account RPC JSON")?;
    if let Some(error) = envelope.error {
        bail!("Solana RPC error {}: {}", error.code, error.message);
    }

    let result = envelope
        .result
        .context("Solana full-account RPC response missing result")?;
    let accounts = result
        .value
        .into_iter()
        .map(|account| {
            account
                .map(|account| {
                    if account.data.1 != "base64" {
                        bail!("unsupported Solana account encoding: {}", account.data.1);
                    }
                    let data = BASE64
                        .decode(account.data.0)
                        .context("invalid base64 Solana account data")?;
                    Ok(AccountData {
                        owner: account.owner,
                        data,
                    })
                })
                .transpose()
        })
        .collect::<Result<Vec<_>>>()?;

    Ok(AccountBatch {
        slot: result.context.slot,
        accounts,
    })
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
    fn builds_full_account_request_with_optional_min_context_slot() {
        let addresses = vec!["A".to_owned(), "B".to_owned()];
        let request = build_full_accounts_request(&addresses, Some(123)).unwrap();
        assert_eq!(request["method"], "getMultipleAccounts");
        assert_eq!(request["params"][0][0], "A");
        assert_eq!(request["params"][1]["encoding"], "base64");
        assert_eq!(request["params"][1]["commitment"], "confirmed");
        assert_eq!(request["params"][1]["minContextSlot"], 123);

        let request = build_full_accounts_request(&addresses, None).unwrap();
        assert!(request["params"][1].get("minContextSlot").is_none());
    }

    #[test]
    fn full_account_request_rejects_more_than_100_addresses() {
        let addresses = (0..101).map(|i| format!("address-{i}")).collect::<Vec<_>>();
        assert!(build_full_accounts_request(&addresses, None).is_err());
    }

    #[test]
    fn parses_full_accounts_base64_and_preserves_order() {
        let encoded = BASE64.encode([1u8, 2, 3, 4]);
        let body = format!(
            r#"{{"jsonrpc":"2.0","result":{{"context":{{"slot":42}},"value":[{{"owner":"program-a","data":["{encoded}","base64"]}},null]}},"id":1}}"#
        );

        let batch = parse_full_accounts_response(&body).unwrap();
        assert_eq!(batch.slot, 42);
        assert_eq!(batch.accounts.len(), 2);
        assert_eq!(
            batch.accounts[0],
            Some(AccountData {
                owner: "program-a".into(),
                data: vec![1, 2, 3, 4]
            })
        );
        assert_eq!(batch.accounts[1], None);
    }

    #[test]
    fn full_account_parser_rejects_rpc_error_encoding_and_bad_base64() {
        let rpc_error = r#"{"jsonrpc":"2.0","error":{"code":-1,"message":"bad"},"id":1}"#;
        assert!(parse_full_accounts_response(rpc_error).is_err());

        let wrong_encoding = r#"{"jsonrpc":"2.0","result":{"context":{"slot":1},"value":[{"owner":"p","data":["abc","base58"]}]},"id":1}"#;
        assert!(parse_full_accounts_response(wrong_encoding).is_err());

        let bad_base64 = r#"{"jsonrpc":"2.0","result":{"context":{"slot":1},"value":[{"owner":"p","data":["%%%","base64"]}]},"id":1}"#;
        assert!(parse_full_accounts_response(bad_base64).is_err());
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
