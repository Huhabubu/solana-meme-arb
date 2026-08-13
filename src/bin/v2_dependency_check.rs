#[cfg(not(test))]
#[path = "../app.rs"]
mod app;
#[cfg(not(test))]
#[path = "../config.rs"]
mod config;
#[cfg(not(test))]
#[path = "../dependencies.rs"]
mod dependencies;
#[cfg(not(test))]
#[path = "../dex/mod.rs"]
mod dex;
#[cfg(not(test))]
#[path = "../discovery.rs"]
mod discovery;
#[cfg(not(test))]
#[path = "../helius.rs"]
mod helius;
#[cfg(not(test))]
#[path = "../model.rs"]
mod model;
#[cfg(not(test))]
#[path = "../rpc.rs"]
mod rpc;
#[cfg(not(test))]
#[path = "../serde_utils.rs"]
mod serde_utils;
#[cfg(not(test))]
#[path = "../state.rs"]
mod state;
#[cfg(not(test))]
#[path = "../token_account.rs"]
mod token_account;
#[cfg(not(test))]
#[path = "../tokens.rs"]
mod tokens;

#[cfg(not(test))]
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    app::run().await
}

// `cargo test --all-targets` 只验证主项目的单元测试；这个额外 binary 不重复编译同一批 #[cfg(test)] 模块。
#[cfg(test)]
fn main() {}
