use std::{
    collections::HashSet,
    str::FromStr,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{bail, Context, Result};
use orca_whirlpools_client::{get_oracle_address, get_tick_array_address};
use orca_whirlpools_core::TickArrayFacade;
use reqwest::Client;
use solana_pubkey::Pubkey;

use crate::{
    config::HeliusConfig,
    dependencies::{
        meteora_dlmm_dependencies, orca_whirlpool_dependencies, raydium_standard_dependencies,
    },
    dex::{
        meteora::DLMM_PROGRAM_ID,
        meteora_dlmm::{
            bin_array_addresses_for_swap, bitmap_extension_address, build_bin_array_map,
            clock_sysvar_address, decode_bin_array, decode_bitmap_extension, decode_clock,
            decode_lb_pair, is_pool_out_of_liquidity as is_meteora_pool_out_of_liquidity,
            quote_exact_in as quote_meteora_exact_in, quote_mint_account, swap_for_y_for_input,
        },
        orca_whirlpool::{
            decode_oracle, decode_tick_array_or_default, decode_whirlpool, needs_oracle,
            quote_exact_in as quote_orca_exact_in, tick_array_start_indexes,
            ORCA_WHIRLPOOL_PROGRAM_ID,
        },
        raydium_amm::{decode_amm_v4, quote_base_in, RAYDIUM_AMM_V4_PROGRAM_ID},
    },
    discovery::{
        discover_pair, select_monitoring_candidates, MAX_POOLS_PER_DEX, MIN_MONITOR_TVL_USD,
    },
    helius::{check_http, subscribe_accounts_and_wait_for_update, subscribe_and_wait_for_update},
    model::{Dex, PoolInfo},
    opportunity::{
        affected_directed_pool_routes, apply_execution_cost, directed_route_indices,
        evaluate_round_trip, evaluate_round_trip_curve, DirectedPoolRoute, ExecutionCost,
        LiquidityStage, OpportunityEvent, OpportunityEventOutcome, SwapQuote,
    },
    rpc::{fetch_account_owners, fetch_accounts, verify_pool_accounts, PUBLIC_MAINNET_RPC},
    state::{DependencyKind, PoolDependencies, QuoteState, VersionedAccountData},
    token_account::{decode_spl_token_account, decode_spl_token_mint, SPL_TOKEN_PROGRAM_ID},
    tokens::{tracked_tokens, Token, WSOL},
};

const APP_NAME: &str = "solana-meme-arb";
const RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000;
const ORCA_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000;
const METEORA_QUOTE_TEST_INPUT_LAMPORTS: u64 = 10_000_000;
const METEORA_BIN_ARRAY_TAKE_COUNT: u8 = 3;
const DEPENDENCY_WSS_WAIT_SECONDS: u64 = 45;
const ROUND_TRIP_PROBE_LAMPORTS: [u64; 3] = [10_000_000, 50_000_000, 100_000_000];
// V3.4 只用作净利润链路的成本下界：当前假设一笔交易仅 1 个普通签名，
// 并使用 Jito 文档最低 bundle tip。Priority Fee 在 V4 得到真实 CU 结构后再动态估计。
const V3_COST_FLOOR_BASE_FEE_LAMPORTS: u64 = 5_000;
const V3_COST_FLOOR_JITO_TIP_LAMPORTS: u64 = 1_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AppCommand {
    Discover,
    Verify,
    HeliusCheck,
    RaydiumQuoteCheck,
    OrcaQuoteCheck,
    MeteoraQuoteCheck,
    DependencyWssCheck,
    OpportunityWssCheck,
    RoundTripCheck,
}

fn parse_command(value: Option<&str>) -> Result<Option<AppCommand>> {
    match value {
        None | Some("help") | Some("--help") | Some("-h") => Ok(None),
        Some("discover") => Ok(Some(AppCommand::Discover)),
        Some("verify") => Ok(Some(AppCommand::Verify)),
        Some("helius-check") => Ok(Some(AppCommand::HeliusCheck)),
        Some("raydium-quote-check") => Ok(Some(AppCommand::RaydiumQuoteCheck)),
        Some("orca-quote-check") => Ok(Some(AppCommand::OrcaQuoteCheck)),
        Some("meteora-quote-check") => Ok(Some(AppCommand::MeteoraQuoteCheck)),
        Some("dependency-wss-check") => Ok(Some(AppCommand::DependencyWssCheck)),
        Some("opportunity-wss-check") => Ok(Some(AppCommand::OpportunityWssCheck)),
        Some("round-trip-check") => Ok(Some(AppCommand::RoundTripCheck)),
        Some(other) => bail!("unknown command: {other}"),
    }
}

pub async fn run() -> Result<()> {
    let argument = std::env::args().nth(1);
    let Some(command) = parse_command(argument.as_deref())? else {
        println!(
            "Usage: {APP_NAME} <discover|verify|helius-check|raydium-quote-check|orca-quote-check|meteora-quote-check|dependency-wss-check|opportunity-wss-check|round-trip-check>"
        );
        return Ok(());
    };
    let client = Client::builder().user_agent(APP_NAME).build()?;

    match command {
        AppCommand::Discover => run_discover(&client).await,
        AppCommand::Verify => run_verify(&client).await,
        AppCommand::HeliusCheck => run_helius_check(&client).await,
        AppCommand::RaydiumQuoteCheck => run_raydium_quote_check(&client).await,
        AppCommand::OrcaQuoteCheck => run_orca_quote_check(&client).await,
        AppCommand::MeteoraQuoteCheck => run_meteora_quote_check(&client).await,
        AppCommand::DependencyWssCheck => run_dependency_wss_check(&client).await,
        AppCommand::OpportunityWssCheck => run_opportunity_wss_check(&client).await,
        AppCommand::RoundTripCheck => run_round_trip_check(&client).await,
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

fn supported_quote_pools(candidates: &[PoolInfo]) -> Vec<PoolInfo> {
    let mut selected = Vec::with_capacity(3);
    if let Some(pool) = candidates.iter().find(|pool| {
        pool.dex == Dex::Raydium
            && pool.pool_type == "Standard"
            && pool.program_id.as_deref() == Some(RAYDIUM_AMM_V4_PROGRAM_ID)
    }) {
        selected.push(pool.clone());
    }
    if let Some(pool) = candidates.iter().find(|pool| {
        pool.dex == Dex::Orca && pool.program_id.as_deref() == Some(ORCA_WHIRLPOOL_PROGRAM_ID)
    }) {
        selected.push(pool.clone());
    }
    if let Some(pool) = candidates.iter().find(|pool| {
        pool.dex == Dex::MeteoraDlmm && pool.program_id.as_deref() == Some(DLMM_PROGRAM_ID)
    }) {
        selected.push(pool.clone());
    }
    selected
}

fn token_symbol_for_pool(pool: &PoolInfo) -> Result<&'static str> {
    tracked_tokens()
        .iter()
        .find(|token| pool.matches_pair(token.mint, WSOL))
        .map(|token| token.symbol)
        .with_context(|| {
            format!(
                "pool is not part of the tracked token universe: {}",
                pool.address
            )
        })
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
        let addresses = candidates
            .iter()
            .map(|pool| pool.address.clone())
            .collect::<Vec<_>>();
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

async fn run_raydium_quote_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let mut verified = 0usize;
    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        if let Some(pool) = supported_quote_pools(&candidates)
            .into_iter()
            .find(|pool| pool.dex == Dex::Raydium)
        {
            quote_raydium_pool(client, &config, &pool).await?;
            verified += 1;
        }
    }
    if verified == 0 {
        bail!("no Raydium Standard AMM v4 pool was available for live quote verification");
    }
    println!("Raydium AMM v4 local quote verification passed for {verified} tracked pair(s)");
    Ok(())
}

#[derive(Debug)]
struct RaydiumLiveQuote {
    swap: SwapQuote,
    pool_slot: u64,
    vault_slot: u64,
    swap_fee_numerator: u64,
    swap_fee_denominator: u64,
    output_decimals: u64,
}

async fn quote_raydium_pool(client: &Client, config: &HeliusConfig, pool: &PoolInfo) -> Result<()> {
    let live = quote_raydium_pool_amount(
        client,
        config,
        pool,
        WSOL,
        RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS,
    )
    .await?;
    let output_ui = live.swap.amount_out as f64 / 10_f64.powi(live.output_decimals as i32);
    println!(
        "{}/WSOL Raydium Standard verified: pool={} pool_slot={} vault_slot={} fee={}/{} input=0.01 WSOL output_raw={} output_ui={:.8}",
        token_symbol_for_pool(pool)?,
        pool.address,
        live.pool_slot,
        live.vault_slot,
        live.swap_fee_numerator,
        live.swap_fee_denominator,
        live.swap.amount_out,
        output_ui
    );
    Ok(())
}

async fn quote_raydium_pool_amount(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amount_in: u64,
) -> Result<RaydiumLiveQuote> {
    quote_raydium_pool_amounts(client, config, pool, input_mint, &[amount_in])
        .await?
        .pop()
        .context("Raydium single-amount quote batch returned no result")
}

async fn quote_raydium_pool_amounts(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amounts_in: &[u64],
) -> Result<Vec<RaydiumLiveQuote>> {
    if amounts_in.is_empty() {
        bail!("Raydium quote batch must contain at least one amount");
    }
    let pool_batch = fetch_accounts(
        client,
        config.http_url().as_str(),
        std::slice::from_ref(&pool.address),
        None,
    )
    .await?;
    let pool_account = pool_batch
        .accounts
        .first()
        .and_then(Option::as_ref)
        .with_context(|| format!("Raydium pool account missing: {}", pool.address))?;
    if pool_account.owner != RAYDIUM_AMM_V4_PROGRAM_ID {
        bail!("Raydium pool owner mismatch for {}", pool.address);
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
    let coin_vault_data = vault_batch.accounts[0]
        .as_ref()
        .context("Raydium coin vault missing")?;
    let pc_vault_data = vault_batch.accounts[1]
        .as_ref()
        .context("Raydium pc vault missing")?;
    if coin_vault_data.owner != SPL_TOKEN_PROGRAM_ID || pc_vault_data.owner != SPL_TOKEN_PROGRAM_ID
    {
        bail!("Raydium AMM v4 vault is not owned by the classic SPL Token program");
    }
    let coin_vault = decode_spl_token_account(&coin_vault_data.data)?;
    let pc_vault = decode_spl_token_account(&pc_vault_data.data)?;
    if coin_vault.mint != state.coin_mint || pc_vault.mint != state.pc_mint {
        bail!("Raydium vault mint does not match decoded pool state");
    }

    let timestamp = unix_timestamp()?;
    let mut results = Vec::with_capacity(amounts_in.len());
    for &amount_in in amounts_in {
        let quote = quote_base_in(
            &state,
            coin_vault.amount,
            pc_vault.amount,
            input_mint,
            amount_in,
            timestamp,
        )?;
        let output_decimals = if quote.output_mint == state.coin_mint {
            state.coin_decimals
        } else {
            state.pc_decimals
        };
        let swap = SwapQuote::new(
            pool.dex,
            pool.address.clone(),
            quote.input_mint,
            quote.output_mint,
            quote.amount_in,
            quote.amount_out,
            pool_batch.slot,
        )?;
        results.push(RaydiumLiveQuote {
            swap,
            pool_slot: pool_batch.slot,
            vault_slot: vault_batch.slot,
            swap_fee_numerator: state.swap_fee_numerator,
            swap_fee_denominator: state.swap_fee_denominator,
            output_decimals,
        });
    }
    Ok(results)
}

async fn run_orca_quote_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let mut verified = 0usize;
    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        if let Some(pool) = supported_quote_pools(&candidates)
            .into_iter()
            .find(|pool| pool.dex == Dex::Orca)
        {
            quote_orca_pool(client, &config, &pool).await?;
            verified += 1;
        }
    }
    if verified == 0 {
        bail!("no Orca Whirlpool was available for live quote verification");
    }
    println!("Orca Whirlpool local quote verification passed for {verified} tracked pair(s)");
    Ok(())
}

#[derive(Debug)]
struct OrcaLiveQuote {
    swap: SwapQuote,
    tick_spacing: String,
    fee_rate: String,
    adaptive_fee: bool,
    output_decimals: u8,
}

async fn quote_orca_pool(client: &Client, config: &HeliusConfig, pool: &PoolInfo) -> Result<()> {
    let live =
        quote_orca_pool_amount(client, config, pool, WSOL, ORCA_QUOTE_TEST_INPUT_LAMPORTS).await?;
    let output_ui = live.swap.amount_out as f64 / 10_f64.powi(i32::from(live.output_decimals));
    println!(
        "{}/WSOL Orca Whirlpool verified: pool={} snapshot_slot={} tick_spacing={} fee_rate={} adaptive_fee={} input=0.01 WSOL output_raw={} output_ui={:.8}",
        token_symbol_for_pool(pool)?,
        pool.address,
        live.swap.snapshot_slot,
        live.tick_spacing,
        live.fee_rate,
        live.adaptive_fee,
        live.swap.amount_out,
        output_ui
    );
    Ok(())
}

async fn quote_orca_pool_amount(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amount_in: u64,
) -> Result<OrcaLiveQuote> {
    quote_orca_pool_amounts(client, config, pool, input_mint, &[amount_in])
        .await?
        .pop()
        .context("Orca single-amount quote batch returned no result")
}

async fn quote_orca_pool_amounts(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amounts_in: &[u64],
) -> Result<Vec<OrcaLiveQuote>> {
    if amounts_in.is_empty() {
        bail!("Orca quote batch must contain at least one amount");
    }
    let program_id =
        Pubkey::from_str(ORCA_WHIRLPOOL_PROGRAM_ID).context("invalid Orca Whirlpool program id")?;
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
                .map_err(|error| anyhow::anyhow!("failed to derive Orca TickArray PDA: {error}"))
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
        bail!("Orca snapshot account count mismatch");
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
        bail!(
            "Orca tick array dependency changed while building coherent snapshot; retry required"
        );
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
    let tick_arrays: [TickArrayFacade; 5] = tick_facades
        .try_into()
        .map_err(|_| anyhow::anyhow!("Orca snapshot did not produce exactly five TickArrays"))?;

    let mint_a_account = accounts[6].as_ref().context("Orca token mint A missing")?;
    let mint_b_account = accounts[7].as_ref().context("Orca token mint B missing")?;
    if mint_a_account.owner != SPL_TOKEN_PROGRAM_ID || mint_b_account.owner != SPL_TOKEN_PROGRAM_ID
    {
        bail!("Orca live quote currently requires classic SPL Token mints");
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

    let (output_mint, output_decimals) = if input_mint == mint_a {
        (mint_b, mint_b_state.decimals)
    } else if input_mint == mint_b {
        (mint_a, mint_a_state.decimals)
    } else {
        bail!("Orca input mint is not part of the decoded pool");
    };
    let timestamp = unix_timestamp()?;
    let mut results = Vec::with_capacity(amounts_in.len());
    for &amount_in in amounts_in {
        let quote = quote_orca_exact_in(
            &whirlpool,
            tick_arrays,
            oracle,
            input_mint,
            amount_in,
            timestamp,
        )?;
        let swap = SwapQuote::new(
            pool.dex,
            pool.address.clone(),
            input_mint,
            output_mint.clone(),
            amount_in,
            quote.token_est_out,
            snapshot.slot,
        )?;
        results.push(OrcaLiveQuote {
            swap,
            tick_spacing: whirlpool.tick_spacing.to_string(),
            fee_rate: whirlpool.fee_rate.to_string(),
            adaptive_fee,
            output_decimals,
        });
    }
    Ok(results)
}

async fn quote_supported_pool_amounts(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amounts_in: &[u64],
) -> Result<Vec<Option<SwapQuote>>> {
    match pool.dex {
        Dex::Raydium => Ok(quote_raydium_pool_amounts(
            client, config, pool, input_mint, amounts_in,
        )
        .await?
        .into_iter()
        .map(|quote| Some(quote.swap))
        .collect()),
        Dex::Orca => Ok(
            quote_orca_pool_amounts(client, config, pool, input_mint, amounts_in)
                .await?
                .into_iter()
                .map(|quote| Some(quote.swap))
                .collect(),
        ),
        Dex::MeteoraDlmm => Ok(quote_meteora_pool_amounts(
            client, config, pool, input_mint, amounts_in,
        )
        .await?
        .into_iter()
        .map(|quote| quote.map(|quote| quote.swap))
        .collect()),
        Dex::MeteoraDammV2 => {
            bail!("Meteora DAMM v2 is not part of the V3 quoteable universe")
        }
    }
}

fn v3_cost_floor() -> ExecutionCost {
    ExecutionCost {
        base_fee_lamports: V3_COST_FLOOR_BASE_FEE_LAMPORTS,
        jito_tip_lamports: V3_COST_FLOOR_JITO_TIP_LAMPORTS,
        ..ExecutionCost::ZERO
    }
}

async fn evaluate_live_route_events(
    client: &Client,
    config: &HeliusConfig,
    route: &DirectedPoolRoute,
    execution_cost: ExecutionCost,
) -> Result<Vec<OpportunityEvent>> {
    let first_points = quote_supported_pool_amounts(
        client,
        config,
        &route.first_pool,
        WSOL,
        &ROUND_TRIP_PROBE_LAMPORTS,
    )
    .await?;
    if first_points.len() != ROUND_TRIP_PROBE_LAMPORTS.len() {
        bail!("V3.5 first-leg quote point count mismatch");
    }

    let mut events = Vec::with_capacity(ROUND_TRIP_PROBE_LAMPORTS.len());
    let mut available_first = Vec::new();
    for (index, point) in first_points.into_iter().enumerate() {
        if let Some(quote) = point {
            available_first.push((index, quote));
        } else {
            events.push(OpportunityEvent::insufficient_liquidity(
                route,
                ROUND_TRIP_PROBE_LAMPORTS[index],
                LiquidityStage::FirstLeg,
            )?);
        }
    }

    if !available_first.is_empty() {
        let second_inputs = available_first
            .iter()
            .map(|(_, quote)| quote.amount_out)
            .collect::<Vec<_>>();
        let second_points = quote_supported_pool_amounts(
            client,
            config,
            &route.second_pool,
            &route.token_mint,
            &second_inputs,
        )
        .await?;
        if second_points.len() != available_first.len() {
            bail!("V3.5 second-leg quote point count mismatch");
        }

        for ((index, first_quote), second_point) in available_first.into_iter().zip(second_points) {
            if let Some(second_quote) = second_point {
                let gross = evaluate_round_trip(&first_quote, &second_quote)?;
                let net = apply_execution_cost(&gross, execution_cost)?;
                events.push(OpportunityEvent::evaluated(route, &net)?);
            } else {
                events.push(OpportunityEvent::insufficient_liquidity(
                    route,
                    ROUND_TRIP_PROBE_LAMPORTS[index],
                    LiquidityStage::SecondLeg,
                )?);
            }
        }
    }

    events.sort_by_key(|event| event.input_amount);
    if events.len() != ROUND_TRIP_PROBE_LAMPORTS.len() {
        bail!("V3.5 route did not account for every probe amount");
    }
    Ok(events)
}

async fn run_round_trip_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let cost_floor = ExecutionCost {
        base_fee_lamports: V3_COST_FLOOR_BASE_FEE_LAMPORTS,
        jito_tip_lamports: V3_COST_FLOOR_JITO_TIP_LAMPORTS,
        ..ExecutionCost::ZERO
    };
    let cost_floor_lamports = cost_floor.total_lamports()?;
    let mut verified_routes = 0usize;
    let mut evaluated_points = 0usize;
    let mut unavailable_points = 0usize;
    let mut gross_profitable_points = 0usize;
    let mut net_profitable_points = 0usize;

    println!(
        "V3 execution cost floor: base={} priority={} jito_tip={} other={} total={} lamports; lower-bound research scenario, not a live landing-cost estimate",
        cost_floor.base_fee_lamports,
        cost_floor.priority_fee_lamports,
        cost_floor.jito_tip_lamports,
        cost_floor.other_lamports,
        cost_floor_lamports
    );

    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        let pools = supported_quote_pools(&candidates);
        if pools.len() != 3 {
            bail!(
                "{}/WSOL expected exactly 3 V3 quoteable pools, got {}",
                token.symbol,
                pools.len()
            );
        }
        let routes = directed_route_indices(pools.len());
        if routes.len() != 6 {
            bail!(
                "{}/WSOL expected 6 directed two-pool routes, got {}",
                token.symbol,
                routes.len()
            );
        }

        for (first_index, second_index) in routes {
            let first_pool = &pools[first_index];
            let second_pool = &pools[second_index];
            let first_points = quote_supported_pool_amounts(
                client,
                &config,
                first_pool,
                WSOL,
                &ROUND_TRIP_PROBE_LAMPORTS,
            )
            .await?;
            if first_points.len() != ROUND_TRIP_PROBE_LAMPORTS.len() {
                bail!("V3 first-leg quote point count mismatch");
            }

            let mut first_available = Vec::new();
            let mut first_indices = Vec::new();
            let mut intermediate_inputs = Vec::new();
            for (index, point) in first_points.iter().enumerate() {
                if let Some(quote) = point {
                    first_indices.push(index);
                    intermediate_inputs.push(quote.amount_out);
                    first_available.push(quote.clone());
                } else {
                    println!(
                        "{}/WSOL V3 curve point unavailable: {}->{} input={} stage=first_leg reason=insufficient_liquidity",
                        token.symbol,
                        first_pool.dex,
                        second_pool.dex,
                        ROUND_TRIP_PROBE_LAMPORTS[index]
                    );
                    unavailable_points += 1;
                }
            }
            if first_available.is_empty() {
                bail!("V3 route has no executable first-leg probe amount");
            }
            let first_slot = first_available[0].snapshot_slot;
            if first_available
                .iter()
                .any(|quote| quote.snapshot_slot != first_slot)
            {
                bail!("V3 first-leg curve mixed multiple snapshots");
            }

            let second_points = quote_supported_pool_amounts(
                client,
                &config,
                second_pool,
                token.mint,
                &intermediate_inputs,
            )
            .await?;
            if second_points.len() != first_available.len() {
                bail!("V3 second-leg quote point count mismatch");
            }

            let mut curve_first = Vec::new();
            let mut curve_second = Vec::new();
            let mut curve_indices = Vec::new();
            for ((index, first_quote), second_point) in first_indices
                .into_iter()
                .zip(first_available)
                .zip(second_points)
            {
                if let Some(second_quote) = second_point {
                    curve_indices.push(index);
                    curve_first.push(first_quote);
                    curve_second.push(second_quote);
                } else {
                    println!(
                        "{}/WSOL V3 curve point unavailable: {}->{} input={} stage=second_leg reason=insufficient_liquidity",
                        token.symbol,
                        first_pool.dex,
                        second_pool.dex,
                        ROUND_TRIP_PROBE_LAMPORTS[index]
                    );
                    unavailable_points += 1;
                }
            }
            if curve_first.is_empty() {
                bail!("V3 route has no fully executable probe amount");
            }
            let second_slot = curve_second[0].snapshot_slot;
            if curve_second
                .iter()
                .any(|quote| quote.snapshot_slot != second_slot)
            {
                bail!("V3 second-leg curve mixed multiple snapshots");
            }
            let curve = evaluate_round_trip_curve(&curve_first, &curve_second)?;
            if curve.len() != curve_indices.len() {
                bail!("V3 evaluated curve/index count mismatch");
            }

            for (index, opportunity) in curve_indices.into_iter().zip(curve.iter()) {
                if opportunity.base_mint != WSOL || opportunity.intermediate_mint != token.mint {
                    bail!("V3 round trip produced unexpected mints");
                }
                if opportunity.input_amount != ROUND_TRIP_PROBE_LAMPORTS[index] {
                    bail!("V3 round-trip result no longer matches original probe amount");
                }

                let net = apply_execution_cost(opportunity, cost_floor)?;
                if opportunity.gross_profit_raw > 0 {
                    gross_profitable_points += 1;
                }
                if net.is_profitable() {
                    net_profitable_points += 1;
                }
                if net.net_profit_raw > opportunity.gross_profit_raw {
                    bail!("execution cost unexpectedly improved profit");
                }

                println!(
                    "{}/WSOL V3 curve point: {}->{} input={} token_out={} final={} gross_profit_raw={} gross_return_ppm={} execution_cost_floor={} net_profit_floor_raw={} net_return_floor_ppm={} slots={}..{}",
                    token.symbol,
                    first_pool.dex,
                    second_pool.dex,
                    opportunity.input_amount,
                    opportunity.intermediate_amount,
                    opportunity.final_amount,
                    opportunity.gross_profit_raw,
                    opportunity.gross_return_ppm,
                    net.execution_cost_lamports,
                    net.net_profit_raw,
                    net.net_return_ppm,
                    opportunity.oldest_slot,
                    opportunity.newest_slot
                );
                evaluated_points += 1;
            }
            println!(
                "{}/WSOL V3 curve verified: {}->{} evaluated={} unavailable={} first_slot={} second_slot={}",
                token.symbol,
                first_pool.dex,
                second_pool.dex,
                curve.len(),
                ROUND_TRIP_PROBE_LAMPORTS.len() - curve.len(),
                first_slot,
                second_slot
            );
            verified_routes += 1;
        }
    }

    let expected_routes = tracked_tokens().len() * 6;
    let expected_points = expected_routes * ROUND_TRIP_PROBE_LAMPORTS.len();
    if verified_routes != expected_routes
        || evaluated_points + unavailable_points != expected_points
    {
        bail!(
            "V3 curve verification count mismatch: expected {expected_routes} routes/{expected_points} accounted points, got {verified_routes} routes/{} evaluated/{unavailable_points} unavailable",
            evaluated_points
        );
    }
    if net_profitable_points > gross_profitable_points {
        bail!("net-profitable point count cannot exceed gross-profitable point count");
    }
    println!(
        "V3 all-DEX multi-size round-trip verification passed for {verified_routes} routes: {evaluated_points} evaluated, {unavailable_points} insufficient-liquidity points, {gross_profitable_points} gross-positive, {net_profitable_points} positive under {cost_floor_lamports}-lamport cost floor"
    );
    Ok(())
}

async fn run_meteora_quote_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let mut verified = 0usize;
    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        if let Some(pool) = supported_quote_pools(&candidates)
            .into_iter()
            .find(|pool| pool.dex == Dex::MeteoraDlmm)
        {
            quote_meteora_pool(client, &config, &pool).await?;
            verified += 1;
        }
    }
    if verified == 0 {
        bail!("no Meteora DLMM pool was available for live quote verification");
    }
    println!("Meteora DLMM local quote verification passed for {verified} tracked pair(s)");
    Ok(())
}

#[derive(Debug)]
struct MeteoraLiveQuote {
    swap: SwapQuote,
    active_id: i32,
    bin_array_count: usize,
    swap_for_y: bool,
    fee: u64,
    protocol_fee: u64,
    output_decimals: Option<u8>,
}

async fn quote_meteora_pool(client: &Client, config: &HeliusConfig, pool: &PoolInfo) -> Result<()> {
    let live = quote_meteora_pool_amount(
        client,
        config,
        pool,
        WSOL,
        METEORA_QUOTE_TEST_INPUT_LAMPORTS,
    )
    .await?;
    if let Some(decimals) = live.output_decimals {
        let output_ui = live.swap.amount_out as f64 / 10_f64.powi(i32::from(decimals));
        println!(
            "{}/WSOL Meteora DLMM verified: pool={} snapshot_slot={} active_id={} bin_arrays={} direction={} input=0.01 WSOL output_mint={} output_raw={} output_ui={:.8} fee={} protocol_fee={}",
            token_symbol_for_pool(pool)?,
            pool.address,
            live.swap.snapshot_slot,
            live.active_id,
            live.bin_array_count,
            if live.swap_for_y { "X->Y" } else { "Y->X" },
            live.swap.output_mint,
            live.swap.amount_out,
            output_ui,
            live.fee,
            live.protocol_fee
        );
    } else {
        println!(
            "{}/WSOL Meteora DLMM verified: pool={} snapshot_slot={} active_id={} bin_arrays={} direction={} input=0.01 WSOL output_mint={} output_raw={} output_ui=n/a(non-classic mint) fee={} protocol_fee={}",
            token_symbol_for_pool(pool)?,
            pool.address,
            live.swap.snapshot_slot,
            live.active_id,
            live.bin_array_count,
            if live.swap_for_y { "X->Y" } else { "Y->X" },
            live.swap.output_mint,
            live.swap.amount_out,
            live.fee,
            live.protocol_fee
        );
    }
    Ok(())
}

async fn quote_meteora_pool_amount(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amount_in: u64,
) -> Result<MeteoraLiveQuote> {
    quote_meteora_pool_amounts(client, config, pool, input_mint, &[amount_in])
        .await?
        .pop()
        .context("Meteora single-amount quote batch returned no result")?
        .context("Meteora single-amount quote is unavailable due to insufficient liquidity")
}

async fn quote_meteora_pool_amounts(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    input_mint: &str,
    amounts_in: &[u64],
) -> Result<Vec<Option<MeteoraLiveQuote>>> {
    if amounts_in.is_empty() {
        bail!("Meteora quote batch must contain at least one amount");
    }
    let bitmap_address = bitmap_extension_address(&pool.address)?;
    let initial_addresses = vec![pool.address.clone(), bitmap_address.clone()];
    let initial =
        fetch_accounts(client, config.http_url().as_str(), &initial_addresses, None).await?;
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
    let swap_for_y = swap_for_y_for_input(&initial_lb_pair, input_mint)?;
    let initial_bin_addresses = bin_array_addresses_for_swap(
        &pool.address,
        &initial_lb_pair,
        initial_bitmap.as_ref(),
        swap_for_y,
        METEORA_BIN_ARRAY_TAKE_COUNT,
    )?;
    if initial_bin_addresses.is_empty() {
        bail!("Meteora official helper returned no BinArray for active swap direction");
    }

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
        bail!("Meteora snapshot account count mismatch");
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
    let snapshot_swap_for_y = swap_for_y_for_input(&lb_pair, input_mint)?;
    let snapshot_bin_addresses = bin_array_addresses_for_swap(
        &pool.address,
        &lb_pair,
        bitmap.as_ref(),
        snapshot_swap_for_y,
        METEORA_BIN_ARRAY_TAKE_COUNT,
    )?;
    if snapshot_swap_for_y != swap_for_y || snapshot_bin_addresses != initial_bin_addresses {
        bail!(
            "Meteora BinArray dependency changed while building coherent snapshot; retry required"
        );
    }

    let clock = decode_clock(
        &snapshot.accounts[1]
            .as_ref()
            .context("Solana Clock sysvar account missing")?
            .data,
    )?;
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
    let (output_mint, output_account) = if snapshot_swap_for_y {
        (mint_y, mint_y_account)
    } else {
        (mint_x, mint_x_account)
    };
    let output_decimals = if output_account.owner == SPL_TOKEN_PROGRAM_ID {
        Some(decode_spl_token_mint(&output_account.data)?.decimals)
    } else {
        None
    };

    let mut results = Vec::with_capacity(amounts_in.len());
    for &amount_in in amounts_in {
        let quote = match quote_meteora_exact_in(
            &pool.address,
            &lb_pair,
            amount_in,
            snapshot_swap_for_y,
            bin_arrays.clone(),
            bitmap.as_ref(),
            &clock,
            &quote_mint_x,
            &quote_mint_y,
        ) {
            Ok(quote) => quote,
            Err(error) if is_meteora_pool_out_of_liquidity(&error) => {
                results.push(None);
                continue;
            }
            Err(error) => return Err(error),
        };
        if quote.amount_out == 0 {
            bail!("Meteora official quote returned zero output");
        }
        let swap = SwapQuote::new(
            pool.dex,
            pool.address.clone(),
            input_mint,
            output_mint.clone(),
            amount_in,
            quote.amount_out,
            snapshot.slot,
        )?;
        results.push(Some(MeteoraLiveQuote {
            swap,
            active_id: lb_pair.active_id,
            bin_array_count: snapshot_bin_addresses.len(),
            swap_for_y: snapshot_swap_for_y,
            fee: quote.fee,
            protocol_fee: quote.protocol_fee,
            output_decimals,
        }));
    }
    Ok(results)
}

async fn build_pool_dependencies(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
) -> Result<PoolDependencies> {
    match pool.dex {
        Dex::Raydium => {
            if pool.pool_type != "Standard"
                || pool.program_id.as_deref() != Some(RAYDIUM_AMM_V4_PROGRAM_ID)
            {
                bail!(
                    "unsupported Raydium pool in V2 quoteable universe: {}",
                    pool.address
                );
            }
            let batch = fetch_accounts(
                client,
                config.http_url().as_str(),
                std::slice::from_ref(&pool.address),
                None,
            )
            .await?;
            let account = batch.accounts[0]
                .as_ref()
                .context("Raydium dependency Pool State missing")?;
            if account.owner != RAYDIUM_AMM_V4_PROGRAM_ID {
                bail!("Raydium dependency Pool State owner mismatch");
            }
            raydium_standard_dependencies(pool, &decode_amm_v4(&account.data)?)
        }
        Dex::Orca => {
            let program_id = Pubkey::from_str(ORCA_WHIRLPOOL_PROGRAM_ID)
                .context("invalid Orca Whirlpool program id")?;
            let batch = fetch_accounts(
                client,
                config.http_url().as_str(),
                std::slice::from_ref(&pool.address),
                None,
            )
            .await?;
            let account = batch.accounts[0]
                .as_ref()
                .context("Orca dependency Whirlpool missing")?;
            if account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
                bail!("Orca dependency Whirlpool owner mismatch");
            }
            let whirlpool = decode_whirlpool(&account.data)?;
            let pool_pubkey =
                Pubkey::from_str(&pool.address).context("invalid Orca pool address")?;
            let tick_addresses =
                tick_array_start_indexes(whirlpool.tick_current_index, whirlpool.tick_spacing)
                    .iter()
                    .map(|index| {
                        get_tick_array_address(&pool_pubkey, *index, Some(program_id))
                            .map(|(address, _)| address.to_string())
                            .map_err(|error| {
                                anyhow::anyhow!("failed to derive Orca TickArray PDA: {error}")
                            })
                    })
                    .collect::<Result<Vec<_>>>()?;
            let tick_batch = fetch_accounts(
                client,
                config.http_url().as_str(),
                &tick_addresses,
                Some(batch.slot),
            )
            .await?;
            let mut existing_ticks = Vec::new();
            for (address, account) in tick_addresses.iter().zip(tick_batch.accounts.iter()) {
                if let Some(account) = account {
                    if account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
                        bail!("Orca dependency TickArray owner mismatch: {address}");
                    }
                    existing_ticks.push(address.clone());
                }
            }
            let oracle_address = if needs_oracle(&whirlpool) {
                Some(
                    get_oracle_address(&pool_pubkey, Some(program_id))
                        .map_err(|error| {
                            anyhow::anyhow!("failed to derive Orca Oracle PDA: {error}")
                        })?
                        .0
                        .to_string(),
                )
            } else {
                None
            };
            orca_whirlpool_dependencies(pool, &existing_ticks, oracle_address.as_deref())
        }
        Dex::MeteoraDlmm => {
            let bitmap_address = bitmap_extension_address(&pool.address)?;
            let addresses = vec![pool.address.clone(), bitmap_address.clone()];
            let batch =
                fetch_accounts(client, config.http_url().as_str(), &addresses, None).await?;
            let account = batch.accounts[0]
                .as_ref()
                .context("Meteora dependency LbPair missing")?;
            if account.owner != DLMM_PROGRAM_ID {
                bail!("Meteora dependency LbPair owner mismatch");
            }
            let lb_pair = decode_lb_pair(&account.data)?;
            let bitmap = batch.accounts[1]
                .as_ref()
                .map(|account| {
                    if account.owner != DLMM_PROGRAM_ID {
                        bail!("Meteora dependency bitmap owner mismatch");
                    }
                    decode_bitmap_extension(&account.data)
                })
                .transpose()?;
            let swap_for_y = swap_for_y_for_input(&lb_pair, WSOL)?;
            let bins = bin_array_addresses_for_swap(
                &pool.address,
                &lb_pair,
                bitmap.as_ref(),
                swap_for_y,
                METEORA_BIN_ARRAY_TAKE_COUNT,
            )?;
            meteora_dlmm_dependencies(
                pool,
                &lb_pair,
                &bins,
                batch.accounts[1].as_ref().map(|_| bitmap_address.as_str()),
            )
        }
        Dex::MeteoraDammV2 => {
            bail!("Meteora DAMM v2 is not in the current V2 quoteable universe")
        }
    }
}

async fn build_quote_state(
    client: &Client,
    config: &HeliusConfig,
) -> Result<(QuoteState, Vec<PoolInfo>)> {
    let mut state = QuoteState::new();
    let mut pools = Vec::new();
    let mut seen = HashSet::new();

    for token in tracked_tokens() {
        let (_, candidates) = discover_candidates(client, token).await?;
        for pool in supported_quote_pools(&candidates) {
            if !seen.insert(pool.address.clone()) {
                continue;
            }
            let dependencies = build_pool_dependencies(client, config, &pool).await?;
            state.replace_pool_dependencies(dependencies)?;
            pools.push(pool);
        }
    }
    if pools.is_empty() {
        bail!("V2 quoteable universe is empty");
    }
    preload_state_accounts(client, config, &mut state, &pools, None).await?;
    Ok((state, pools))
}

async fn preload_state_accounts(
    client: &Client,
    config: &HeliusConfig,
    state: &mut QuoteState,
    pools: &[PoolInfo],
    min_context_slot: Option<u64>,
) -> Result<()> {
    let addresses = state.unique_dependency_addresses();
    let batch = fetch_accounts(
        client,
        config.http_url().as_str(),
        &addresses,
        min_context_slot,
    )
    .await?;
    if batch.accounts.len() != addresses.len() {
        bail!("dependency snapshot account count mismatch");
    }
    for (address, account) in addresses.iter().zip(batch.accounts) {
        let account = account.with_context(|| {
            format!("registered quote dependency account is missing: {address}")
        })?;
        state.apply_account_update(
            address,
            VersionedAccountData {
                slot: batch.slot,
                owner: account.owner,
                data: account.data,
            },
        )?;
    }
    for pool in pools {
        let missing = state.missing_accounts_for_pool(&pool.address)?;
        if !missing.is_empty() {
            bail!(
                "pool {} still has missing dependency accounts: {missing:?}",
                pool.address
            );
        }
    }
    Ok(())
}

async fn refresh_pool_dependencies(
    client: &Client,
    config: &HeliusConfig,
    state: &mut QuoteState,
    pool: &PoolInfo,
    min_context_slot: u64,
) -> Result<()> {
    let refreshed = build_pool_dependencies(client, config, pool).await?;
    state.replace_pool_dependencies(refreshed)?;
    let missing = state.missing_accounts_for_pool(&pool.address)?;
    if missing.is_empty() {
        return Ok(());
    }

    let batch = fetch_accounts(
        client,
        config.http_url().as_str(),
        &missing,
        Some(min_context_slot),
    )
    .await?;
    for (address, account) in missing.iter().zip(batch.accounts) {
        let account = account
            .with_context(|| format!("new quote dependency account is missing: {address}"))?;
        state.apply_account_update(
            address,
            VersionedAccountData {
                slot: batch.slot,
                owner: account.owner,
                data: account.data,
            },
        )?;
    }
    Ok(())
}

async fn recompute_pool_quote(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
) -> Result<()> {
    match pool.dex {
        Dex::Raydium => quote_raydium_pool(client, config, pool).await,
        Dex::Orca => quote_orca_pool(client, config, pool).await,
        Dex::MeteoraDlmm => quote_meteora_pool(client, config, pool).await,
        Dex::MeteoraDammV2 => bail!("unsupported V2 quote recompute pool"),
    }
}

async fn run_dependency_wss_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let (mut state, pools) = build_quote_state(client, &config).await?;
    let addresses = state.unique_dependency_addresses();
    let mut accepted_addresses = HashSet::new();
    for pool in &pools {
        let dependencies = state
            .dependencies_for_pool(&pool.address)
            .context("registered pool disappeared from QuoteState")?;
        for account in &dependencies.accounts {
            if matches!(
                account.kind,
                DependencyKind::TokenVault
                    | DependencyKind::TickArray
                    | DependencyKind::Oracle
                    | DependencyKind::BinArray
                    | DependencyKind::BitmapExtension
            ) {
                accepted_addresses.insert(account.address.clone());
            }
        }
    }
    if accepted_addresses.is_empty() {
        bail!("no non-Pool quote dependency accounts are available for WSS verification");
    }

    println!(
        "V2 dependency WSS subscribing to {} unique accounts across {} quoteable pools; waiting for {} non-Pool trigger accounts",
        addresses.len(),
        pools.len(),
        accepted_addresses.len()
    );
    let update = subscribe_accounts_and_wait_for_update(
        &config,
        &addresses,
        &accepted_addresses,
        Duration::from_secs(DEPENDENCY_WSS_WAIT_SECONDS),
    )
    .await?;
    let applied = state.apply_account_update(
        &update.address,
        VersionedAccountData {
            slot: update.slot,
            owner: update.owner.clone(),
            data: update.data.clone(),
        },
    )?;
    if !applied.accepted {
        bail!("received dependency WSS update was older than the local snapshot");
    }
    if applied.affected_pools.is_empty() {
        bail!("dependency WSS update did not map to any pool");
    }

    println!(
        "V2 dependency WSS update verified: address={} slot={} subscription={} affected_pools={:?}",
        update.address, update.slot, update.subscription_id, applied.affected_pools
    );
    for pool_address in applied.affected_pools {
        let dependencies = state
            .dependencies_for_pool(&pool_address)
            .context("affected pool missing from QuoteState")?
            .clone();
        let kind = state
            .dependency_kind(&pool_address, &update.address)
            .context("updated account missing dependency kind")?;
        println!(
            "  trigger pool={} dex={} kind={kind:?}; recomputing quote",
            dependencies.pool.address, dependencies.pool.dex
        );
        recompute_pool_quote(client, &config, &dependencies.pool).await?;
        refresh_pool_dependencies(client, &config, &mut state, &dependencies.pool, update.slot)
            .await?;
        if !state
            .missing_accounts_for_pool(&dependencies.pool.address)?
            .is_empty()
        {
            bail!("refreshed pool dependencies are incomplete after recompute");
        }
    }
    println!(
        "V2 dependency-triggered quote recompute verified; refreshed dependency set is complete"
    );
    Ok(())
}

async fn run_opportunity_wss_check(client: &Client) -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let (mut state, pools) = build_quote_state(client, &config).await?;
    let addresses = state.unique_dependency_addresses();
    let accepted_addresses = addresses.iter().cloned().collect::<HashSet<_>>();
    if accepted_addresses.is_empty() {
        bail!("V3.5 opportunity universe has no quote dependency accounts");
    }

    println!(
        "V3.5 opportunity WSS subscribing to {} unique dependencies across {} quoteable pools",
        addresses.len(),
        pools.len()
    );
    let update = subscribe_accounts_and_wait_for_update(
        &config,
        &addresses,
        &accepted_addresses,
        Duration::from_secs(DEPENDENCY_WSS_WAIT_SECONDS),
    )
    .await?;
    let applied = state.apply_account_update(
        &update.address,
        VersionedAccountData {
            slot: update.slot,
            owner: update.owner.clone(),
            data: update.data.clone(),
        },
    )?;
    if !applied.accepted {
        bail!("V3.5 received dependency update older than local QuoteState");
    }
    if applied.affected_pools.is_empty() {
        bail!("V3.5 dependency update did not map to any affected pool");
    }

    let affected_pools = applied.affected_pools.clone();
    println!(
        "V3.5 trigger: address={} slot={} subscription={} affected_pools={:?}",
        update.address, update.slot, update.subscription_id, affected_pools
    );
    for pool_address in &affected_pools {
        let dependencies = state
            .dependencies_for_pool(pool_address)
            .context("V3.5 affected pool disappeared from QuoteState")?
            .clone();
        let kind = state
            .dependency_kind(pool_address, &update.address)
            .context("V3.5 updated account missing dependency kind")?;
        println!(
            "  affected pool={} dex={} kind={kind:?}; refreshing dynamic dependencies",
            dependencies.pool.address, dependencies.pool.dex
        );
        refresh_pool_dependencies(client, &config, &mut state, &dependencies.pool, update.slot)
            .await?;
        if !state.missing_accounts_for_pool(pool_address)?.is_empty() {
            bail!("V3.5 refreshed affected-pool dependencies are incomplete");
        }
    }

    let routes = affected_directed_pool_routes(&pools, &affected_pools, WSOL)?;
    if routes.is_empty() {
        bail!("V3.5 affected pools produced no related arbitrage routes");
    }
    let cost_floor = v3_cost_floor();
    let mut event_count = 0usize;
    let mut evaluated_count = 0usize;
    let mut unavailable_count = 0usize;
    let mut net_positive_count = 0usize;

    for route in &routes {
        let token = tracked_tokens()
            .iter()
            .find(|token| token.mint == route.token_mint)
            .context("V3.5 route token is outside tracked universe")?;
        let events = evaluate_live_route_events(client, &config, route, cost_floor).await?;
        for event in &events {
            match &event.outcome {
                OpportunityEventOutcome::Evaluated {
                    intermediate_amount,
                    final_amount,
                    gross_profit_raw,
                    gross_return_ppm,
                    execution_cost_lamports,
                    net_profit_raw,
                    net_return_ppm,
                    oldest_slot,
                    newest_slot,
                } => {
                    evaluated_count += 1;
                    if *net_profit_raw > 0 {
                        net_positive_count += 1;
                    }
                    println!(
                        "{}/WSOL V3.5 event: {}->{} input={} token_out={} final={} gross_profit_raw={} gross_return_ppm={} execution_cost={} net_profit_raw={} net_return_ppm={} slots={}..{}",
                        token.symbol,
                        event.first_dex,
                        event.second_dex,
                        event.input_amount,
                        intermediate_amount,
                        final_amount,
                        gross_profit_raw,
                        gross_return_ppm,
                        execution_cost_lamports,
                        net_profit_raw,
                        net_return_ppm,
                        oldest_slot,
                        newest_slot
                    );
                }
                OpportunityEventOutcome::InsufficientLiquidity { stage } => {
                    unavailable_count += 1;
                    println!(
                        "{}/WSOL V3.5 event: {}->{} input={} status=insufficient_liquidity stage={stage:?}",
                        token.symbol,
                        event.first_dex,
                        event.second_dex,
                        event.input_amount
                    );
                }
            }
        }
        event_count += events.len();
    }

    let expected_events = routes.len() * ROUND_TRIP_PROBE_LAMPORTS.len();
    if event_count != expected_events || evaluated_count + unavailable_count != event_count {
        bail!(
            "V3.5 opportunity event accounting mismatch: routes={} expected_events={} events={} evaluated={} unavailable={}",
            routes.len(),
            expected_events,
            event_count,
            evaluated_count,
            unavailable_count
        );
    }
    println!(
        "V3.5 dependency-triggered opportunity recompute verified: {} affected pool(s), {} related route(s), {} event(s), {} evaluated, {} insufficient-liquidity, {} net-positive",
        affected_pools.len(),
        routes.len(),
        event_count,
        evaluated_count,
        unavailable_count,
        net_positive_count
    );
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

    fn pool(dex: Dex, pool_type: &str, program_id: &str, address: &str) -> PoolInfo {
        PoolInfo {
            dex,
            address: address.into(),
            pool_type: pool_type.into(),
            program_id: Some(program_id.into()),
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd: 1_000.0,
        }
    }

    #[test]
    fn command_parser_accepts_dependency_wss_and_rejects_unknown_command() {
        assert_eq!(
            parse_command(Some("dependency-wss-check")).unwrap(),
            Some(AppCommand::DependencyWssCheck)
        );
        assert_eq!(
            parse_command(Some("opportunity-wss-check")).unwrap(),
            Some(AppCommand::OpportunityWssCheck)
        );
        assert_eq!(
            parse_command(Some("round-trip-check")).unwrap(),
            Some(AppCommand::RoundTripCheck)
        );
        assert_eq!(parse_command(None).unwrap(), None);
        assert!(parse_command(Some("definitely-unknown")).is_err());
    }

    #[test]
    fn supported_pool_selection_keeps_only_current_quote_engines() {
        let candidates = vec![
            pool(Dex::Raydium, "Concentrated", "clmm", "ray-clmm"),
            pool(
                Dex::Raydium,
                "Standard",
                RAYDIUM_AMM_V4_PROGRAM_ID,
                "ray-standard",
            ),
            pool(Dex::Orca, "whirlpool", ORCA_WHIRLPOOL_PROGRAM_ID, "orca"),
            pool(Dex::MeteoraDlmm, "DLMM", DLMM_PROGRAM_ID, "meteora"),
            pool(Dex::MeteoraDammV2, "DAMM v2", "damm", "damm"),
        ];
        let selected = supported_quote_pools(&candidates);
        assert_eq!(selected.len(), 3);
        assert_eq!(selected[0].address, "ray-standard");
        assert_eq!(selected[1].address, "orca");
        assert_eq!(selected[2].address, "meteora");
    }

    #[test]
    fn quote_probe_sizes_are_stable() {
        assert_eq!(RAYDIUM_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
        assert_eq!(ORCA_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
        assert_eq!(METEORA_QUOTE_TEST_INPUT_LAMPORTS, 10_000_000);
        assert_eq!(METEORA_BIN_ARRAY_TAKE_COUNT, 3);
        assert_eq!(
            ROUND_TRIP_PROBE_LAMPORTS,
            [10_000_000, 50_000_000, 100_000_000]
        );
    }
}
