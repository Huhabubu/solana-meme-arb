mod config;
mod dex;
mod discovery;
mod helius;
mod model;
mod rpc;
mod serde_utils;
mod token_account;
mod tokens;

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use reqwest::Client;

use config::HeliusConfig;
use dex::raydium_amm::{decode_amm_v4, quote_base_in, RAYDIUM_AMM_V4_PROGRAM_ID};
use discovery::{
    discover_pair, select_monitoring_candidates, MAX_POOLS_PER_DEX, MIN_MONITOR_TVL_USD,
};
use helius::{check_http, subscribe_and_wait_for_update};
use model::{Dex, PoolInfo};
use rpc::{fetch_account_owners, fetch_accounts, verify_pool_accounts, PUBLIC_MAINNET_RPC};
use token_account::{decode_spl_token_account, SPL_TOKEN_PROGRAM_ID};
use tokens::{tracked_tokens, Token, WSOL};

const APP_NAME: &str = "solana-meme-arb";
const RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000; // 0.01 WSOL

#[tokio::main]
async fn main() -> Result<()> {
    let command = std::env::args().nth(1).unwrap_or_else(|| "help".into());
    let client = Client::builder().user_agent(APP_NAME).build()?;

    match command.as_str() {
        "discover" => run_discover(&client).await,
        "verify" => run_verify(&client).await,
        "helius-check" => run_helius_check(&client).await,
        "raydium-quote-check" => run_raydium_quote_check(&client).await,
        _ => {
            println!("Usage: {APP_NAME} <discover|verify|helius-check|raydium-quote-check>");
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

async fn run_helius_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let version = check_http(client, &config).await?;
    println!("Helius HTTP verified: Solana core {version}");

    let mut pools = Vec::new();
    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        pools.extend(candidates);
    }

    println!("Helius WSS subscribing to {} candidate pools", pools.len());
    let update = subscribe_and_wait_for_update(&config, &pools, Duration::from_secs(45)).await?;
    println!(
        "Helius WSS verified: update slot={} dex={} pool={} subscription={}",
        update.slot, update.pool.dex, update.pool.address, update.subscription_id
    );

    Ok(())
}

/// 真实读取 Raydium Standard 池及两个 vault，并用链上程序相同的 SwapBaseInV2 数学计算 0.01 WSOL 报价。
async fn run_raydium_quote_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before Unix epoch")?
        .as_secs();
    let mut verified = 0usize;

    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        let pool = candidates.iter().find(|pool| {
            pool.dex == Dex::Raydium
                && pool.program_id.as_deref() == Some(RAYDIUM_AMM_V4_PROGRAM_ID)
                && pool.pool_type == "Standard"
        });
        let Some(pool) = pool else {
            println!(
                "{}/WSOL: no selected Raydium Standard AMM v4 pool",
                token.symbol
            );
            continue;
        };

        let pool_batch = fetch_accounts(
            client,
            config.http_url().as_str(),
            std::slice::from_ref(&pool.address),
            None,
        )
        .await?;
        let pool_account = pool_batch
            .accounts
            .into_iter()
            .next()
            .flatten()
            .with_context(|| format!("Raydium pool account missing: {}", pool.address))?;
        if pool_account.owner != RAYDIUM_AMM_V4_PROGRAM_ID {
            bail!(
                "Raydium pool owner mismatch: expected {}, got {}",
                RAYDIUM_AMM_V4_PROGRAM_ID,
                pool_account.owner
            );
        }

        let state = decode_amm_v4(&pool_account.data)?;
        if !pool.matches_pair(&state.coin_mint, &state.pc_mint) {
            bail!(
                "Raydium decoded mints do not match discovery metadata for {}",
                pool.address
            );
        }

        let vault_addresses = vec![state.coin_vault.clone(), state.pc_vault.clone()];
        let vault_batch = fetch_accounts(
            client,
            config.http_url().as_str(),
            &vault_addresses,
            Some(pool_batch.slot),
        )
        .await?;
        if vault_batch.accounts.len() != 2 {
            bail!("Raydium vault RPC returned unexpected account count");
        }
        let mut vaults = vault_batch.accounts.into_iter();
        let coin_vault_data = vaults
            .next()
            .flatten()
            .context("Raydium coin vault missing")?;
        let pc_vault_data = vaults
            .next()
            .flatten()
            .context("Raydium pc vault missing")?;
        if coin_vault_data.owner != SPL_TOKEN_PROGRAM_ID
            || pc_vault_data.owner != SPL_TOKEN_PROGRAM_ID
        {
            bail!("Raydium AMM v4 vault is not owned by the classic SPL Token program");
        }

        let coin_vault = decode_spl_token_account(&coin_vault_data.data)?;
        let pc_vault = decode_spl_token_account(&pc_vault_data.data)?;
        if coin_vault.mint != state.coin_mint || pc_vault.mint != state.pc_mint {
            bail!("Raydium vault mint does not match decoded pool state");
        }

        let quote = quote_base_in(
            &state,
            coin_vault.amount,
            pc_vault.amount,
            WSOL,
            RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS,
            now,
        )?;
        let output_decimals = if quote.output_mint == state.coin_mint {
            state.coin_decimals
        } else {
            state.pc_decimals
        };
        let output_ui = quote.amount_out as f64 / 10_f64.powi(output_decimals as i32);

        println!(
            "{}/WSOL Raydium Standard verified: pool={} pool_slot={} vault_slot={} fee={}/{} input=0.01 WSOL output_raw={} output_ui={:.8}",
            token.symbol,
            pool.address,
            pool_batch.slot,
            vault_batch.slot,
            state.swap_fee_numerator,
            state.swap_fee_denominator,
            quote.amount_out,
            output_ui
        );
        verified += 1;
    }

    if verified == 0 {
        bail!("no Raydium Standard AMM v4 pool was available for live quote verification");
    }
    println!("Raydium AMM v4 local quote verification passed for {verified} tracked pair(s)");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_name_is_stable() {
        assert_eq!(APP_NAME, "solana-meme-arb");
    }

    #[test]
    fn raydium_live_quote_probe_uses_point_zero_one_sol() {
        assert_eq!(RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
    }
}
