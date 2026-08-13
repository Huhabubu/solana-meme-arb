use std::collections::HashMap;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::time::timeout;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::config::HeliusConfig;
use crate::model::PoolInfo;

const SUBSCRIPTION_COMMITMENT: &str = "confirmed";

#[derive(Debug, Clone, PartialEq)]
pub struct AccountUpdate {
    pub pool: PoolInfo,
    pub subscription_id: u64,
    pub slot: u64,
}

#[derive(Debug, Deserialize)]
struct VersionEnvelope {
    result: Option<VersionResult>,
    error: Option<RpcError>,
}

#[derive(Debug, Deserialize)]
struct VersionResult {
    #[serde(rename = "solana-core")]
    solana_core: String,
}

#[derive(Debug, Deserialize)]
struct RpcError {
    code: i64,
    message: String,
}

#[derive(Debug, PartialEq, Eq)]
enum ServerEvent {
    SubscriptionAck {
        request_id: u64,
        subscription_id: u64,
    },
    AccountNotification {
        subscription_id: u64,
        slot: u64,
    },
    RpcError {
        request_id: Option<u64>,
        code: i64,
        message: String,
    },
    Other,
}

struct SubscriptionTracker {
    pending: HashMap<u64, PoolInfo>,
    active: HashMap<u64, PoolInfo>,
}

impl SubscriptionTracker {
    fn new(pools: &[PoolInfo]) -> Self {
        let pending = pools
            .iter()
            .enumerate()
            .map(|(index, pool)| ((index + 1) as u64, pool.clone()))
            .collect();
        Self {
            pending,
            active: HashMap::new(),
        }
    }

    fn acknowledge(&mut self, request_id: u64, subscription_id: u64) -> Result<()> {
        let pool = self.pending.remove(&request_id).with_context(|| {
            format!("unknown or duplicate subscription request id {request_id}")
        })?;
        if self.active.insert(subscription_id, pool).is_some() {
            bail!("duplicate subscription id {subscription_id}");
        }
        Ok(())
    }

    fn resolve(&self, subscription_id: u64) -> Result<&PoolInfo> {
        self.active
            .get(&subscription_id)
            .with_context(|| format!("notification for unknown subscription id {subscription_id}"))
    }

    fn all_acknowledged(&self) -> bool {
        self.pending.is_empty()
    }
}

pub async fn check_http(client: &Client, config: &HeliusConfig) -> Result<String> {
    let request = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getVersion"
    });

    // 不把含 API Key 的 URL 放进 anyhow 错误链，避免 CI 日志意外泄露密钥。
    let response = client
        .post(config.http_url())
        .json(&request)
        .send()
        .await
        .map_err(|_| anyhow::anyhow!("Helius HTTP request failed"))?;
    let status = response.status();
    if !status.is_success() {
        bail!("Helius HTTP returned status {status}");
    }
    let body = response
        .text()
        .await
        .map_err(|_| anyhow::anyhow!("failed to read Helius HTTP response"))?;

    parse_version_response(&body)
}

fn parse_version_response(body: &str) -> Result<String> {
    let envelope: VersionEnvelope =
        serde_json::from_str(body).context("invalid Helius getVersion JSON")?;
    if let Some(error) = envelope.error {
        bail!("Helius RPC error {}: {}", error.code, error.message);
    }

    Ok(envelope
        .result
        .context("Helius getVersion response missing result")?
        .solana_core)
}

fn build_account_subscribe_request(request_id: u64, address: &str) -> String {
    json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "accountSubscribe",
        "params": [
            address,
            {
                "encoding": "base64",
                "commitment": SUBSCRIPTION_COMMITMENT
            }
        ]
    })
    .to_string()
}

fn parse_server_event(text: &str) -> Result<ServerEvent> {
    let value: Value = serde_json::from_str(text).context("invalid Helius WSS JSON")?;

    if let Some(error) = value.get("error") {
        return Ok(ServerEvent::RpcError {
            request_id: value.get("id").and_then(Value::as_u64),
            code: error
                .get("code")
                .and_then(Value::as_i64)
                .context("WSS error missing code")?,
            message: error
                .get("message")
                .and_then(Value::as_str)
                .context("WSS error missing message")?
                .to_owned(),
        });
    }

    if value.get("method").and_then(Value::as_str) == Some("accountNotification") {
        let params = value
            .get("params")
            .context("accountNotification missing params")?;
        return Ok(ServerEvent::AccountNotification {
            subscription_id: params
                .get("subscription")
                .and_then(Value::as_u64)
                .context("accountNotification missing subscription id")?,
            slot: params
                .pointer("/result/context/slot")
                .and_then(Value::as_u64)
                .context("accountNotification missing slot")?,
        });
    }

    if let Some(request_id) = value.get("id").and_then(Value::as_u64) {
        let subscription_id = value
            .get("result")
            .and_then(Value::as_u64)
            .context("subscription response missing numeric result")?;
        return Ok(ServerEvent::SubscriptionAck {
            request_id,
            subscription_id,
        });
    }

    Ok(ServerEvent::Other)
}

/// 订阅全部候选池；只有全部收到订阅确认且至少收到一次真实账户更新后才返回成功。
pub async fn subscribe_and_wait_for_update(
    config: &HeliusConfig,
    pools: &[PoolInfo],
    wait_timeout: Duration,
) -> Result<AccountUpdate> {
    if pools.is_empty() {
        bail!("cannot subscribe to an empty pool list");
    }

    let wss_url = config.wss_url();
    let (mut socket, _) = connect_async(wss_url.as_str())
        .await
        .map_err(|_| anyhow::anyhow!("Helius WSS connection failed"))?;
    let mut tracker = SubscriptionTracker::new(pools);

    for (index, pool) in pools.iter().enumerate() {
        let request_id = (index + 1) as u64;
        let request = build_account_subscribe_request(request_id, &pool.address);
        socket
            .send(Message::Text(request.into()))
            .await
            .map_err(|_| anyhow::anyhow!("failed to send Helius subscription request"))?;
    }

    timeout(wait_timeout, async {
        let mut first_update: Option<AccountUpdate> = None;

        loop {
            if tracker.all_acknowledged() {
                if let Some(update) = first_update.take() {
                    return Ok(update);
                }
            }

            let message = socket
                .next()
                .await
                .context("Helius WSS closed before V1 verification completed")?
                .map_err(|_| anyhow::anyhow!("Helius WSS receive error"))?;

            match message {
                Message::Text(text) => match parse_server_event(text.as_ref())? {
                    ServerEvent::SubscriptionAck {
                        request_id,
                        subscription_id,
                    } => tracker.acknowledge(request_id, subscription_id)?,
                    ServerEvent::AccountNotification {
                        subscription_id,
                        slot,
                    } => {
                        let pool = tracker.resolve(subscription_id)?.clone();
                        first_update.get_or_insert(AccountUpdate {
                            pool,
                            subscription_id,
                            slot,
                        });
                    }
                    ServerEvent::RpcError {
                        request_id,
                        code,
                        message,
                    } => {
                        bail!("Helius WSS RPC error request={request_id:?} code={code}: {message}");
                    }
                    ServerEvent::Other => {}
                },
                Message::Close(_) => bail!("Helius WSS closed before V1 verification completed"),
                Message::Ping(payload) => {
                    socket
                        .send(Message::Pong(payload))
                        .await
                        .map_err(|_| anyhow::anyhow!("failed to reply to Helius WSS ping"))?;
                }
                Message::Binary(_) | Message::Pong(_) | Message::Frame(_) => {}
            }
        }
    })
    .await
    .context("timed out waiting for Helius subscriptions and account update")?
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Dex;

    fn pool(address: &str) -> PoolInfo {
        PoolInfo {
            dex: Dex::Orca,
            address: address.into(),
            pool_type: "whirlpool".into(),
            program_id: Some("program".into()),
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd: 1_000.0,
        }
    }

    #[test]
    fn parses_version_response() {
        let body = r#"{"jsonrpc":"2.0","result":{"solana-core":"3.1.8","feature-set":1},"id":1}"#;
        assert_eq!(parse_version_response(body).unwrap(), "3.1.8");
    }

    #[test]
    fn rejects_version_rpc_error() {
        let body = r#"{"jsonrpc":"2.0","error":{"code":-1,"message":"bad key"},"id":1}"#;
        assert!(parse_version_response(body).is_err());
    }

    #[test]
    fn builds_confirmed_base64_account_subscription() {
        let request: Value =
            serde_json::from_str(&build_account_subscribe_request(7, "pool-a")).unwrap();
        assert_eq!(request["id"], 7);
        assert_eq!(request["method"], "accountSubscribe");
        assert_eq!(request["params"][0], "pool-a");
        assert_eq!(request["params"][1]["encoding"], "base64");
        assert_eq!(request["params"][1]["commitment"], "confirmed");
    }

    #[test]
    fn parses_subscription_ack() {
        let event = parse_server_event(r#"{"jsonrpc":"2.0","result":123,"id":7}"#).unwrap();
        assert_eq!(
            event,
            ServerEvent::SubscriptionAck {
                request_id: 7,
                subscription_id: 123
            }
        );
    }

    #[test]
    fn parses_account_notification() {
        let text = r#"{
            "jsonrpc":"2.0",
            "method":"accountNotification",
            "params":{"result":{"context":{"slot":999},"value":{}},"subscription":123}
        }"#;
        assert_eq!(
            parse_server_event(text).unwrap(),
            ServerEvent::AccountNotification {
                subscription_id: 123,
                slot: 999
            }
        );
    }

    #[test]
    fn parses_wss_rpc_error() {
        let event = parse_server_event(
            r#"{"jsonrpc":"2.0","error":{"code":-32602,"message":"bad params"},"id":2}"#,
        )
        .unwrap();
        assert_eq!(
            event,
            ServerEvent::RpcError {
                request_id: Some(2),
                code: -32602,
                message: "bad params".into()
            }
        );
    }

    #[test]
    fn tracker_maps_request_to_subscription_and_pool() {
        let pools = vec![pool("pool-a"), pool("pool-b")];
        let mut tracker = SubscriptionTracker::new(&pools);
        assert!(!tracker.all_acknowledged());

        tracker.acknowledge(1, 101).unwrap();
        tracker.acknowledge(2, 202).unwrap();
        assert!(tracker.all_acknowledged());
        assert_eq!(tracker.resolve(101).unwrap().address, "pool-a");
        assert_eq!(tracker.resolve(202).unwrap().address, "pool-b");
    }

    #[test]
    fn tracker_rejects_unknown_request_and_subscription() {
        let pools = vec![pool("pool-a")];
        let mut tracker = SubscriptionTracker::new(&pools);
        assert!(tracker.acknowledge(99, 101).is_err());
        assert!(tracker.resolve(999).is_err());
    }

    #[test]
    fn tracker_rejects_duplicate_subscription_id() {
        let pools = vec![pool("pool-a"), pool("pool-b")];
        let mut tracker = SubscriptionTracker::new(&pools);
        tracker.acknowledge(1, 101).unwrap();
        assert!(tracker.acknowledge(2, 101).is_err());
    }
}
