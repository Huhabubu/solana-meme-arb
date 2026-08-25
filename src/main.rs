mod app;
mod config;
mod dependencies;
mod dex;
mod discovery;
mod event_monitor;
mod helius;
mod model;
mod monitor;
mod opportunity;
mod persistence;
mod quote_context;
mod rpc;
mod serde_utils;
mod state;
mod token_account;
mod tokens;

fn inherit_legacy_monitor_env(old_name: &str, new_name: &str) {
    if std::env::var_os(new_name).is_none() {
        if let Some(value) = std::env::var_os(old_name) {
            std::env::set_var(new_name, value);
        }
    }
}

fn prepare_event_monitor_env() {
    // 兼容已有 workflow / VPS 启动参数；策略语义已经切换为交易事件数，
    // 但旧部署脚本无需和本次代码改动原子同步。
    inherit_legacy_monitor_env("OPPORTUNITY_MONITOR_UPDATES", "EVENT_MONITOR_MAX_EVENTS");
    inherit_legacy_monitor_env("OPPORTUNITY_MONITOR_MAX_SECONDS", "EVENT_MONITOR_MAX_SECONDS");
    inherit_legacy_monitor_env(
        "OPPORTUNITY_MONITOR_UPDATE_TIMEOUT_SECONDS",
        "EVENT_MONITOR_UPDATE_TIMEOUT_SECONDS",
    );
    inherit_legacy_monitor_env(
        "OPPORTUNITY_MONITOR_MAX_RECONNECTS",
        "EVENT_MONITOR_MAX_RECONNECTS",
    );
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    match std::env::args().nth(1).as_deref() {
        Some("opportunity-monitor") | Some("event-monitor") => {
            prepare_event_monitor_env();
            event_monitor::run().await
        }
        _ => app::run().await,
    }
}
