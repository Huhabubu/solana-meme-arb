use std::time::Duration;

use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use reqwest::{header::RETRY_AFTER, Client, StatusCode};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::time::sleep;

use crate::model::PoolInfo;

pub const PUBLIC_MAINNET_RPC: &str = "https://api.mainnet-beta.solana.com";

const MIN_CONTEXT_SLOT_NOT_REACHED_CODE: i64 = -32016;
const MIN_CONTEXT_SLOT_MAX_RETRIES: usize = 2;
const MIN_CONTEXT_SLOT_RETRY_BASE_MS: u64 = 200;
const TRANSIENT_HTTP_MAX_RETRIES: usize = 4;
const TRANSIENT_HTTP_RETRY_BASE_MS: u64 = 1_000;
const TRANSIENT_HTTP_RETRY_MAX_MS: u64 = 30_000;

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
///
/// `-32016` 表示节点还没有追上调用方要求的 `minContextSlot`。这种情况下允许短暂、有限重试，
/// 但每次都复用完全相同的请求，因此不会降低 `minContextSlot` 或回退读取旧状态。
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
    let mut min_context_retries = 0usize;
    let mut transient_http_retries = 0usize;

    loop {
        let response = client
            .post(rpc_url)
            .json(&request)
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("Solana RPC full-account request failed"))?;
        let status = response.status();

        if !status.is_success() {
            if should_retry_http_status(status, transient_http_retries) {
                let retry_after = response
                    .headers()
                    .get(RETRY_AFTER)
                    .and_then(|value| value.to_str().ok());
                let delay_ms = transient_http_retry_delay_ms(transient_http_retries, retry_after);
                transient_http_retries += 1;
                sleep(Duration::from_millis(delay_ms)).await;
                continue;
            }
            bail!("Solana RPC full-account request returned status {status}");
        }

        let body = response
            .text()
            .await
            .map_err(|_| anyhow::anyhow!("failed to read Solana RPC full-account response"))?;

        if let Some(code) = full_accounts_rpc_error_code(&body) {
            if should_retry_min_context_slot_error(
                code,
                min_context_retries,
                min_context_slot.is_some(),
            ) {
                sleep(Duration::from_millis(min_context_retry_delay_ms(
                    min_context_retries,
                )))
                .await;
                min_context_retries += 1;
                continue;
            }
        }

        return parse_full_accounts_response(&body);
    }
}

fn build_full_accounts_request(
    addresses: &[String],
    min_context_slot: Option<u64>,
) -> Result<Value> {
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

fn full_accounts_rpc_error_code(body: &str) -> Option<i64> {
    serde_json::from_str::<FullRpcEnvelope>(body)
        .ok()?
        .error
        .map(|error| error.code)
}

fn should_retry_min_context_slot_error(
    code: i64,
    retry_count: usize,
    has_min_context_slot: bool,
) -> bool {
    has_min_context_slot
        && code == MIN_CONTEXT_SLOT_NOT_REACHED_CODE
        && retry_count < MIN_CONTEXT_SLOT_MAX_RETRIES
}

fn min_context_retry_delay_ms(retry_count: usize) -> u64 {
    MIN_CONTEXT_SLOT_RETRY_BASE_MS * (retry_count as u64 + 1)
}

fn should_retry_http_status(status: StatusCode, retry_count: usize) -> bool {
    retry_count < TRANSIENT_HTTP_MAX_RETRIES
        && matches!(status.as_u16(), 408 | 429 | 500 | 502 | 503 | 504)
}

fn transient_http_retry_delay_ms(retry_count: usize, retry_after: Option<&str>) -> u64 {
    if let Some(seconds) = retry_after.and_then(|value| value.parse::<u64>().ok()) {
        return seconds
            .saturating_mul(1_000)
            .min(TRANSIENT_HTTP_RETRY_MAX_MS);
    }

    TRANSIENT_HTTP_RETRY_BASE_MS
        .saturating_mul(1_u64 << retry_count.min(5))
        .min(TRANSIENT_HTTP_RETRY_MAX_MS)
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
    fn retry_policy_is_narrow_and_bounded() {
        assert!(should_retry_min_context_slot_error(-32016, 0, true));
        assert!(should_retry_min_context_slot_error(-32016, 1, true));
        assert!(!should_retry_min_context_slot_error(-32016, 2, true));
        assert!(!should_retry_min_context_slot_error(-32016, 0, false));
        assert!(!should_retry_min_context_slot_error(-32602, 0, true));
        assert_eq!(min_context_retry_delay_ms(0), 200);
        assert_eq!(min_context_retry_delay_ms(1), 400);
    }

    #[test]
    fn transient_http_retry_policy_is_bounded_and_selective() {
        for code in [408, 429, 500, 502, 503, 504] {
            let status = StatusCode::from_u16(code).unwrap();
            assert!(should_retry_http_status(status, 0));
            assert!(should_retry_http_status(status, 3));
            assert!(!should_retry_http_status(status, 4));
        }
        for code in [400, 401, 403, 404, 409, 422] {
            assert!(!should_retry_http_status(
                StatusCode::from_u16(code).unwrap(),
                0
            ));
        }
    }

    #[test]
    fn transient_http_backoff_prefers_retry_after_and_caps_delay() {
        assert_eq!(transient_http_retry_delay_ms(0, None), 1_000);
        assert_eq!(transient_http_retry_delay_ms(1, None), 2_000);
        assert_eq!(transient_http_retry_delay_ms(3, None), 8_000);
        assert_eq!(transient_http_retry_delay_ms(0, Some("2")), 2_000);
        assert_eq!(transient_http_retry_delay_ms(0, Some("999")), 30_000);
        assert_eq!(transient_http_retry_delay_ms(2, Some("invalid")), 4_000);
    }

    #[test]
    fn extracts_full_account_rpc_error_code_without_hiding_parse_errors() {
        let lagging = r#"{"jsonrpc":"2.0","error":{"code":-32016,"message":"Minimum context slot has not been reached"},"id":1}"#;
        assert_eq!(full_accounts_rpc_error_code(lagging), Some(-32016));
        assert_eq!(full_accounts_rpc_error_code("not-json"), None);
        assert!(parse_full_accounts_response("not-json").is_err());
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
