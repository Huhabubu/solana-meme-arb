mod config;
mod dex;
mod discovery;
mod helius;
mod model;
mod rpc;
mod serde_utils;
mod token_account;
mod tokens;

use std::{
    str::FromStr,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{bail, Context, Result};
use orca_whirlpools_client::{get_oracle_address, get_tick_array_address};
use orca_whirlpools_core::TickArrayFacade;
use reqwest::Client;
use solana_pubkey::Pubkey;

use config::HeliusConfig;
use dex::{
    meteora::DLMM_PROGRAM_ID,
    meteora_dlmm::{
        bin_array_addresses_for_swap, bitmap_extension_address, build_bin_array_map,
        clock_sysvar_address, decode_bin_array, decode_bitmap_extension, decode_clock,
        decode_lb_pair, quote_exact_in as quote_meteora_exact_in, quote_mint_account,
        swap_for_y_for_input,
    },
    orca_whirlpool::{
        decode_oracle, decode_tick_array_or_default, decode_whirlpool, needs_oracle,
        quote_exact_in as quote_orca_exact_in, tick_array_start_indexes, ORCA_WHIRLPOOL_PROGRAM_ID,
    },
    raydium_amm::{decode_amm_v4, quote_base_in, RAYDIUM_AMM_V4_PROGRAM_ID},
};
use discovery::{
    discover_pair, select_monitoring_candidates, MAX_POOLS_PER_DEX, MIN_MONITOR_TVL_USD,
};
use helius::{check_http, subscribe_and_wait_for_update};
use model::{Dex, PoolInfo};
use rpc::{fetch_account_owners, fetch_accounts, verify_pool_accounts, PUBLIC_MAINNET_RPC};
use token_account::{decode_spl_token_account, decode_spl_token_mint, SPL_TOKEN_PROGRAM_ID};
use tokens::{tracked_tokens, Token, WSOL};

const APP_NAME: &str = "solana-meme-arb";
const RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000; // 0.01 WSOL
const ORCA_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000; // 0.01 WSOL
const METEORA_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000; // 0.01 WSOL
const METEORA_BIN_ARRAY_TAKE_COUNT: u8 = 3;

#[tokio::main]
async fn main() -> Result<()> {
    let command = std::env::args().nth(1).unwrap_or_else(|| "help".into());
    let client = Client::builder().user_agent(APP_NAME).build()?;

    match command.as_str() {
        "discover" => run_discover(&client).await,
        "verify" => run_verify(&client).await,
        "helius-check" => run_helius_check(&client).await,
        "raydium-quote-check" => run_raydium_quote_check(&client).await,
        "orca-quote-check" => run_orca_quote_check(&client).await,
        "meteora-quote-check" => run_meteora_quote_check(&client).await,
        _ => {
            println!(
                "Usage: {APP_NAME} <discover|verify|helius-check|raydium-quote-check|orca-quote-check|meteora-quote-check>"
            );
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
    let now = unix_timestamp()?;
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

/// 用 Orca 官方 Rust 解码器和 `orca_whirlpools_core` 报价引擎，真实验证 0.01 WSOL 的 Whirlpool 报价。
async fn run_orca_quote_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let program_id =
        Pubkey::from_str(ORCA_WHIRLPOOL_PROGRAM_ID).context("invalid Orca Whirlpool program id")?;
    let now = unix_timestamp()?;
    let mut verified = 0usize;

    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        let pool = candidates.iter().find(|pool| {
            pool.dex == Dex::Orca && pool.program_id.as_deref() == Some(ORCA_WHIRLPOOL_PROGRAM_ID)
        });
        let Some(pool) = pool else {
            println!("{}/WSOL: no selected Orca Whirlpool", token.symbol);
            continue;
        };

        // 第一次只读取 Whirlpool，用它确定本次报价依赖哪些 TickArray / Oracle 地址。
        let initial_batch = fetch_accounts(
            client,
            config.http_url().as_str(),
            std::slice::from_ref(&pool.address),
            None,
        )
        .await?;
        let initial_account = initial_batch
            .accounts
            .first()
            .and_then(Option::as_ref)
            .with_context(|| format!("Orca Whirlpool account missing: {}", pool.address))?;
        if initial_account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
            bail!("Orca Whirlpool owner mismatch for {}", pool.address);
        }
        let initial_whirlpool = decode_whirlpool(&initial_account.data)?;
        let pool_pubkey = Pubkey::from_str(&pool.address).context("invalid Orca pool address")?;
        let initial_tick_indexes = tick_array_start_indexes(
            initial_whirlpool.tick_current_index,
            initial_whirlpool.tick_spacing,
        );
        let tick_addresses = initial_tick_indexes
            .iter()
            .map(|index| {
                get_tick_array_address(&pool_pubkey, *index, Some(program_id))
                    .map(|(address, _)| address.to_string())
                    .map_err(|error| {
                        anyhow::anyhow!("failed to derive Orca TickArray PDA: {error}")
                    })
            })
            .collect::<Result<Vec<_>>>()?;
        let adaptive_fee = needs_oracle(&initial_whirlpool);
        let oracle_address = if adaptive_fee {
            Some(
                get_oracle_address(&pool_pubkey, Some(program_id))
                    .map_err(|error| anyhow::anyhow!("failed to derive Orca Oracle PDA: {error}"))?
                    .0
                    .to_string(),
            )
        } else {
            None
        };

        // 第二次把 Whirlpool 本身和所有报价依赖账户放进同一个 getMultipleAccounts，获得同一 context slot 的快照。
        let mut snapshot_addresses = Vec::with_capacity(9);
        snapshot_addresses.push(pool.address.clone());
        snapshot_addresses.extend(tick_addresses.iter().cloned());
        snapshot_addresses.push(initial_whirlpool.token_mint_a.to_string());
        snapshot_addresses.push(initial_whirlpool.token_mint_b.to_string());
        if let Some(address) = &oracle_address {
            snapshot_addresses.push(address.clone());
        }
        let snapshot = fetch_accounts(
            client,
            config.http_url().as_str(),
            &snapshot_addresses,
            Some(initial_batch.slot),
        )
        .await?;
        let accounts = &snapshot.accounts;
        let expected_len = if adaptive_fee { 9 } else { 8 };
        if accounts.len() != expected_len {
            bail!(
                "Orca snapshot account count mismatch: expected {}, got {}",
                expected_len,
                accounts.len()
            );
        }

        let pool_account = accounts[0]
            .as_ref()
            .context("Orca snapshot Whirlpool account missing")?;
        if pool_account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
            bail!("Orca snapshot Whirlpool owner mismatch");
        }
        let whirlpool = decode_whirlpool(&pool_account.data)?;
        let mint_a = whirlpool.token_mint_a.to_string();
        let mint_b = whirlpool.token_mint_b.to_string();
        if !pool.matches_pair(&mint_a, &mint_b) {
            bail!(
                "Orca decoded mints do not match discovery metadata for {}",
                pool.address
            );
        }
        let snapshot_tick_indexes =
            tick_array_start_indexes(whirlpool.tick_current_index, whirlpool.tick_spacing);
        if snapshot_tick_indexes != initial_tick_indexes {
            bail!("Orca tick array dependency changed while building coherent snapshot; retry required");
        }
        if needs_oracle(&whirlpool) != adaptive_fee {
            bail!("Orca adaptive-fee configuration changed while building snapshot");
        }

        let mut tick_facades = Vec::with_capacity(5);
        for index in 0..5 {
            if let Some(tick_account) = accounts[index + 1].as_ref() {
                if tick_account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
                    bail!("Orca TickArray owner mismatch at {}", tick_addresses[index]);
                }
            }
            tick_facades.push(decode_tick_array_or_default(
                accounts[index + 1]
                    .as_ref()
                    .map(|account| account.data.as_slice()),
                snapshot_tick_indexes[index],
            )?);
        }
        let tick_arrays: [TickArrayFacade; 5] = tick_facades.try_into().map_err(|_| {
            anyhow::anyhow!("Orca snapshot did not produce exactly five TickArrays")
        })?;

        let mint_a_account = accounts[6].as_ref().context("Orca token mint A missing")?;
        let mint_b_account = accounts[7].as_ref().context("Orca token mint B missing")?;
        if mint_a_account.owner != SPL_TOKEN_PROGRAM_ID
            || mint_b_account.owner != SPL_TOKEN_PROGRAM_ID
        {
            bail!(
                "Orca live quote currently requires classic SPL Token mints; Token-2022 transfer fees are not yet implemented"
            );
        }
        let mint_a_state = decode_spl_token_mint(&mint_a_account.data)?;
        let mint_b_state = decode_spl_token_mint(&mint_b_account.data)?;
        if !mint_a_state.is_initialized || !mint_b_state.is_initialized {
            bail!("Orca pool references an uninitialized SPL Token mint");
        }

        let oracle = if adaptive_fee {
            let oracle_account = accounts[8]
                .as_ref()
                .context("Orca adaptive-fee Oracle missing")?;
            if oracle_account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
                bail!("Orca Oracle owner mismatch");
            }
            let oracle = decode_oracle(&oracle_account.data)?;
            if oracle.whirlpool != pool_pubkey {
                bail!("Orca Oracle points to a different Whirlpool");
            }
            Some(oracle.into())
        } else {
            None
        };

        let quote = quote_orca_exact_in(
            &whirlpool,
            tick_arrays,
            oracle,
            WSOL,
            ORCA_QUOTE_TEST_INPUT_LAMPORTS,
            now,
        )?;
        let output_decimals = if mint_a == WSOL {
            mint_b_state.decimals
        } else if mint_b == WSOL {
            mint_a_state.decimals
        } else {
            bail!("selected Orca pool does not contain WSOL after decoding");
        };
        let output_ui = quote.token_est_out as f64 / 10_f64.powi(i32::from(output_decimals));

        println!(
            "{}/WSOL Orca Whirlpool verified: pool={} snapshot_slot={} tick_spacing={} fee_rate={} adaptive_fee={} input=0.01 WSOL output_raw={} output_ui={:.8}",
            token.symbol,
            pool.address,
            snapshot.slot,
            whirlpool.tick_spacing,
            whirlpool.fee_rate,
            adaptive_fee,
            quote.token_est_out,
            output_ui
        );
        verified += 1;
    }

    if verified == 0 {
        bail!("no Orca Whirlpool was available for live quote verification");
    }
    println!("Orca Whirlpool local quote verification passed for {verified} tracked pair(s)");
    Ok(())
}

/// 真实读取 Meteora DLMM 的 LbPair、bitmap extension、Clock、Mint 和当前方向 BinArray，
/// 再调用 Meteora 官方 Rust `commons::quote_exact_in` 计算 0.01 WSOL 报价。
async fn run_meteora_quote_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let mut verified = 0usize;

    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        let pool = candidates.iter().find(|pool| {
            pool.dex == Dex::MeteoraDlmm && pool.program_id.as_deref() == Some(DLMM_PROGRAM_ID)
        });
        let Some(pool) = pool else {
            println!("{}/WSOL: no selected Meteora DLMM pool", token.symbol);
            continue;
        };

        let bitmap_address = bitmap_extension_address(&pool.address)?;
        let initial_addresses = vec![pool.address.clone(), bitmap_address.clone()];
        let initial =
            fetch_accounts(client, config.http_url().as_str(), &initial_addresses, None).await?;
        if initial.accounts.len() != 2 {
            bail!("Meteora initial snapshot returned unexpected account count");
        }

        let initial_pool_account = initial.accounts[0]
            .as_ref()
            .with_context(|| format!("Meteora LbPair account missing: {}", pool.address))?;
        if initial_pool_account.owner != DLMM_PROGRAM_ID {
            bail!("Meteora LbPair owner mismatch for {}", pool.address);
        }
        let initial_lb_pair = decode_lb_pair(&initial_pool_account.data)?;
        let initial_mint_x = initial_lb_pair.token_x_mint.to_string();
        let initial_mint_y = initial_lb_pair.token_y_mint.to_string();
        if !pool.matches_pair(&initial_mint_x, &initial_mint_y) {
            bail!(
                "Meteora decoded mints do not match discovery metadata for {}",
                pool.address
            );
        }

        let initial_bitmap = initial.accounts[1]
            .as_ref()
            .map(|account| {
                if account.owner != DLMM_PROGRAM_ID {
                    bail!("Meteora bitmap extension owner mismatch");
                }
                decode_bitmap_extension(&account.data)
            })
            .transpose()?;
        let initial_swap_for_y = swap_for_y_for_input(&initial_lb_pair, WSOL)?;
        let initial_bin_addresses = bin_array_addresses_for_swap(
            &pool.address,
            &initial_lb_pair,
            initial_bitmap.as_ref(),
            initial_swap_for_y,
            METEORA_BIN_ARRAY_TAKE_COUNT,
        )?;
        if initial_bin_addresses.is_empty() {
            bail!("Meteora official helper returned no BinArray for active swap direction");
        }

        // 第二次请求把所有报价依赖账户放在同一个 getMultipleAccounts 快照中。
        let mut snapshot_addresses = Vec::with_capacity(5 + initial_bin_addresses.len());
        snapshot_addresses.push(pool.address.clone());
        snapshot_addresses.push(clock_sysvar_address());
        snapshot_addresses.push(initial_mint_x.clone());
        snapshot_addresses.push(initial_mint_y.clone());
        snapshot_addresses.push(bitmap_address.clone());
        snapshot_addresses.extend(initial_bin_addresses.iter().cloned());

        let snapshot = fetch_accounts(
            client,
            config.http_url().as_str(),
            &snapshot_addresses,
            Some(initial.slot),
        )
        .await?;
        if snapshot.accounts.len() != snapshot_addresses.len() {
            bail!(
                "Meteora snapshot account count mismatch: expected {}, got {}",
                snapshot_addresses.len(),
                snapshot.accounts.len()
            );
        }

        let lb_pair_account = snapshot.accounts[0]
            .as_ref()
            .context("Meteora snapshot LbPair account missing")?;
        if lb_pair_account.owner != DLMM_PROGRAM_ID {
            bail!("Meteora snapshot LbPair owner mismatch");
        }
        let lb_pair = decode_lb_pair(&lb_pair_account.data)?;
        let mint_x = lb_pair.token_x_mint.to_string();
        let mint_y = lb_pair.token_y_mint.to_string();
        if !pool.matches_pair(&mint_x, &mint_y) {
            bail!("Meteora snapshot mints no longer match selected pair");
        }

        let bitmap = snapshot.accounts[4]
            .as_ref()
            .map(|account| {
                if account.owner != DLMM_PROGRAM_ID {
                    bail!("Meteora snapshot bitmap extension owner mismatch");
                }
                decode_bitmap_extension(&account.data)
            })
            .transpose()?;
        let swap_for_y = swap_for_y_for_input(&lb_pair, WSOL)?;
        let snapshot_bin_addresses = bin_array_addresses_for_swap(
            &pool.address,
            &lb_pair,
            bitmap.as_ref(),
            swap_for_y,
            METEORA_BIN_ARRAY_TAKE_COUNT,
        )?;
        if swap_for_y != initial_swap_for_y || snapshot_bin_addresses != initial_bin_addresses {
            bail!("Meteora BinArray dependency changed while building coherent snapshot; retry required");
        }

        let clock_account = snapshot.accounts[1]
            .as_ref()
            .context("Solana Clock sysvar account missing")?;
        let clock = decode_clock(&clock_account.data)?;
        let mint_x_account = snapshot.accounts[2]
            .as_ref()
            .context("Meteora token X mint account missing")?;
        let mint_y_account = snapshot.accounts[3]
            .as_ref()
            .context("Meteora token Y mint account missing")?;
        let quote_mint_x = quote_mint_account(&mint_x_account.owner, &mint_x_account.data)?;
        let quote_mint_y = quote_mint_account(&mint_y_account.owner, &mint_y_account.data)?;

        let mut bin_entries = Vec::with_capacity(snapshot_bin_addresses.len());
        for (index, address) in snapshot_bin_addresses.iter().enumerate() {
            let account = snapshot.accounts[index + 5]
                .as_ref()
                .with_context(|| format!("Meteora BinArray account missing: {address}"))?;
            if account.owner != DLMM_PROGRAM_ID {
                bail!("Meteora BinArray owner mismatch: {address}");
            }
            bin_entries.push((address.clone(), decode_bin_array(&account.data)?));
        }
        let bin_arrays = build_bin_array_map(bin_entries)?;

        let quote = quote_meteora_exact_in(
            &pool.address,
            &lb_pair,
            METEORA_QUOTE_TEST_INPUT_LAMPORTS,
            swap_for_y,
            bin_arrays,
            bitmap.as_ref(),
            &clock,
            &quote_mint_x,
            &quote_mint_y,
        )?;
        if quote.amount_out == 0 {
            bail!("Meteora official quote returned zero output");
        }

        let output_mint = if swap_for_y { &mint_y } else { &mint_x };
        let output_account = if swap_for_y {
            mint_y_account
        } else {
            mint_x_account
        };
        let output_ui = if output_account.owner == SPL_TOKEN_PROGRAM_ID {
            let output_state = decode_spl_token_mint(&output_account.data)?;
            Some(quote.amount_out as f64 / 10_f64.powi(i32::from(output_state.decimals)))
        } else {
            None
        };

        match output_ui {
            Some(output_ui) => println!(
                "{}/WSOL Meteora DLMM verified: pool={} snapshot_slot={} active_id={} bin_arrays={} direction={} input=0.01 WSOL output_mint={} output_raw={} output_ui={:.8} fee={} protocol_fee={}",
                token.symbol,
                pool.address,
                snapshot.slot,
                lb_pair.active_id,
                snapshot_bin_addresses.len(),
                if swap_for_y { "X->Y" } else { "Y->X" },
                output_mint,
                quote.amount_out,
                output_ui,
                quote.fee,
                quote.protocol_fee
            ),
            None => println!(
                "{}/WSOL Meteora DLMM verified: pool={} snapshot_slot={} active_id={} bin_arrays={} direction={} input=0.01 WSOL output_mint={} output_raw={} output_ui=n/a(non-classic mint) fee={} protocol_fee={}",
                token.symbol,
                pool.address,
                snapshot.slot,
                lb_pair.active_id,
                snapshot_bin_addresses.len(),
                if swap_for_y { "X->Y" } else { "Y->X" },
                output_mint,
                quote.amount_out,
                quote.fee,
                quote.protocol_fee
            ),
        }
        verified += 1;
    }

    if verified == 0 {
        bail!("no Meteora DLMM pool was available for live quote verification");
    }
    println!("Meteora DLMM local quote verification passed for {verified} tracked pair(s)");
    Ok(())
}

fn unix_timestamp() -> Result<u64> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before Unix epoch")?
        .as_secs())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_name_is_stable() {
        assert_eq!(APP_NAME, "solana-meme-arb");
    }

    #[test]
    fn live_quote_probes_use_point_zero_one_sol() {
        assert_eq!(RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
        assert_eq!(ORCA_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
        assert_eq!(METEORA_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
        assert_eq!(METEORA_BIN_ARRAY_TAKE_COUNT, 3);
    }
}
