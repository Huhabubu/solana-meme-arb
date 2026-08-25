use anyhow::{bail, Context, Result};

const HELIUS_API_KEY_ENV: &str = "HELIUS_API_KEY";

/// Helius API Key 只保存在内存中，不实现 Debug/Display，避免日志意外输出密钥。
pub struct HeliusConfig {
    api_key: String,
}

impl HeliusConfig {
    pub fn from_env() -> Result<Self> {
        let api_key = std::env::var(HELIUS_API_KEY_ENV)
            .with_context(|| format!("missing environment variable {HELIUS_API_KEY_ENV}"))?;
        Self::new(api_key)
    }

    pub fn new(api_key: impl Into<String>) -> Result<Self> {
        let api_key = api_key.into().trim().to_owned();
        if api_key.is_empty() {
            bail!("Helius API key cannot be empty");
        }

        Ok(Self { api_key })
    }

    pub fn http_url(&self) -> String {
        format!("https://mainnet.helius-rpc.com/?api-key={}", self.api_key)
    }

    pub fn wss_url(&self) -> String {
        format!("wss://mainnet.helius-rpc.com/?api-key={}", self.api_key)
    }

    /// V1 事件监控暂时使用 Helius Enhanced Transactions 把 WSS 交易签名分类为 SWAP。
    /// URL 只在请求边界构造，调用方不得把它写入日志，避免泄露 API Key。
    pub fn enhanced_transactions_url(&self) -> String {
        format!(
            "https://api-mainnet.helius-rpc.com/v0/transactions/?api-key={}",
            self.api_key
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_empty_api_key() {
        assert!(HeliusConfig::new("   ").is_err());
    }

    #[test]
    fn trims_key_and_builds_expected_endpoints() {
        let config = HeliusConfig::new("  test-key  ").unwrap();
        assert_eq!(
            config.http_url(),
            "https://mainnet.helius-rpc.com/?api-key=test-key"
        );
        assert_eq!(
            config.wss_url(),
            "wss://mainnet.helius-rpc.com/?api-key=test-key"
        );
        assert_eq!(
            config.enhanced_transactions_url(),
            "https://api-mainnet.helius-rpc.com/v0/transactions/?api-key=test-key"
        );
    }
}
