use anyhow::{bail, Context, Result};

const HELIUS_API_KEY_V2_ENV: &str = "HELIUS_API_KEY_V2";
const HELIUS_API_KEY_ENV: &str = "HELIUS_API_KEY";

/// Helius API Key 只保存在内存中，不实现 Debug/Display，避免日志意外输出密钥。
pub struct HeliusConfig {
    api_key: String,
}

impl HeliusConfig {
    pub fn from_env() -> Result<Self> {
        // 新项目/新额度优先使用 V2；保留旧变量只为本地部署脚本平滑迁移。
        // CI / live smoke 只注入 HELIUS_API_KEY_V2，避免意外回退到已耗尽的旧 key。
        let api_key = match std::env::var(HELIUS_API_KEY_V2_ENV) {
            Ok(value) => value,
            Err(std::env::VarError::NotPresent) => std::env::var(HELIUS_API_KEY_ENV).with_context(
                || {
                    format!(
                        "missing environment variable {HELIUS_API_KEY_V2_ENV} (legacy fallback: {HELIUS_API_KEY_ENV})"
                    )
                },
            )?,
            Err(error) => {
                return Err(error).with_context(|| format!("failed to read {HELIUS_API_KEY_V2_ENV}"));
            }
        };
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

    /// 事件触发已经走 Helius Standard WSS；这里仍只负责把签名分类为 SWAP。
    /// Enhanced Transactions 已被 Helius 标记为 deprecated，后续应由本地 DEX decoder 替代。
    /// 当前接口不支持 processed，只能显式使用 confirmed 避免默认 finalized 的额外等待。
    /// URL 只在请求边界构造，调用方不得把它写入日志，避免泄露 API Key。
    pub fn enhanced_transactions_url(&self) -> String {
        format!(
            "https://api-mainnet.helius-rpc.com/v0/transactions/?api-key={}&commitment=confirmed",
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
            "https://api-mainnet.helius-rpc.com/v0/transactions/?api-key=test-key&commitment=confirmed"
        );
    }
}
