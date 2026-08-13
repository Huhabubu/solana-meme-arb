mod dex;
mod discovery;
mod model;
mod serde_utils;
mod tokens;

use anyhow::{bail, Result};
use reqwest::Client;

use discovery::discover_pair;
use tokens::{tracked_tokens, WSOL};

const APP_NAME: &str = "solana-meme-arb";

#[tokio::main]
async fn main() -> Result<()> {
    let command = std::env::args().nth(1).unwrap_or_else(|| "help".into());
    if command != "discover" {
        println!("Usage: {APP_NAME} discover");
        return Ok(());
    }

    let client = Client::builder().user_agent(APP_NAME).build()?;

    for token in tracked_tokens() {
        let pools = discover_pair(&client, token.mint, WSOL).await?;
        if pools.is_empty() {
            bail!("{} / WSOL: no exact pools found", token.symbol);
        }

        println!("\n========== {}/WSOL ==========" , token.symbol);
        for pool in pools {
            println!(
                "{:<16} {:<44} TVL ${:>12.2}  {}",
                pool.dex, pool.address, pool.tvl_usd, pool.pool_type
            );
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_name_is_stable() {
        assert_eq!(APP_NAME, "solana-meme-arb");
    }
}
