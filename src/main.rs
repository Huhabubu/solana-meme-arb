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

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    match std::env::args().nth(1).as_deref() {
        Some("opportunity-monitor") | Some("event-monitor") => event_monitor::run().await,
        _ => app::run().await,
    }
}
