mod app;
mod config;
mod dependencies;
mod dex;
mod discovery;
mod helius;
mod model;
mod rpc;
mod serde_utils;
mod state;
mod token_account;
mod tokens;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    app::run().await
}
