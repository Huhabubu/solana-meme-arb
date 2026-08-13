mod dex;
mod discovery;
mod model;
mod rpc;
mod serde_utils;
mod tokens;

use anyhow::{bail, Result};
use reqwest::Client;

use discovery::{
    discover_pair, select_monitoring_candidates, MAX_POOLS_PER_DEX, MIN_MONITOR_TVL_USD,
};
use model::PoolInfo;
use rpc::{fetch_account_owners, verify_pool_accounts, PUBLIC_MAINNET_RPC};
use tokens::{tracked_tokens, Token, WSOL};

const APP_NAME: &str = "solana-meme-arb";

#[tokio::main]
async fn main() -> Result<()> {
    let command = std::env::args().nth(1).unwrap_or_else(|| "help".into());
    let client = Client::builder().user_agent(APP_NAME).build()?;

    match command.as_str() {
        "discover" => run_discover(&client).await,
        "verify" => run_verify(&client).await,
        _ => {
            println!("Usage: {APP_NAME} <discover|verify>");
            Ok(())
        }
    }
}

async fn discover_candidates(client: &Client, token: &Token) -> Result<(usize, Vec<PoolInfo>)> {
    let discovered = discover_pair(client, token.mint, WSOL).await?;
    if discovered.is_empty() {
        bail!("{} / WSOL: no exact pools found", token.symbol);
    }

    let candidates =
        select_monitoring_candidates(&discovered, MIN_MONITOR_TVL_USD, MAX_POOLS_PER_DEX);
    if candidates.is_empty() {
        bail!(
            "{} / WSOL: pools exist but none meet monitoring threshold",
            token.symbol
        );
    }

    Ok((discovered.len(), candidates))
}

async fn run_discover(client: &Client) -> Result<()> {
    for token in tracked_tokens() {
        let (discovered_count, candidates) = discover_candidates(client, token).await?;
        println!(
            "\n========== {}/WSOL: {} discovered, {} selected ==========",
            token.symbol,
            discovered_count,
            candidates.len()
        );

        for pool in candidates {
            println!(
                "{:<16} {:<44} TVL ${:>12.2}  {}",
                pool.dex, pool.address, pool.tvl_usd, pool.pool_type
            );
        }
    }

    Ok(())
}

async fn run_verify(client: &Client) -> Result<()> {
    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        let addresses: Vec<String> = candidates.iter().map(|pool| pool.address.clone()).collect();
        let owners = fetch_account_owners(client, PUBLIC_MAINNET_RPC, &addresses).await?;
        verify_pool_accounts(&candidates, &owners)?;

        println!(
            "{}/WSOL: verified {} pool accounts on Solana Mainnet",
            token.symbol,
            candidates.len()
        );
        for (pool, owner) in candidates.iter().zip(owners.iter()) {
            println!(
                "  {:<16} {} owner={}",
                pool.dex,
                pool.address,
                owner.as_deref().unwrap_or("missing")
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
