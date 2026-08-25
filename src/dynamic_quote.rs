use std::{
    collections::HashSet,
    str::FromStr,
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{bail, Context, Result};
use orca_whirlpools_client::{get_oracle_address, get_tick_array_address};
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
            bin_array_addresses_for_swap, bitmap_extension_address, clock_sysvar_address,
            decode_bitmap_extension, decode_clock, decode_lb_pair, swap_for_y_for_input,
        },
        orca_whirlpool::{
            decode_whirlpool, needs_oracle, tick_array_start_indexes, ORCA_WHIRLPOOL_PROGRAM_ID,
        },
        raydium_amm::{decode_amm_v4, RAYDIUM_AMM_V4_PROGRAM_ID},
    },
    model::{Dex, PoolInfo},
    opportunity::{
        affected_directed_pool_routes, apply_execution_cost, evaluate_round_trip,
        DirectedPoolRoute, ExecutionCost, LiquidityStage, OpportunityEvent, SwapQuote,
    },
    quote_context::{QuoteContextCache, QuoteRuntime},
    rpc::fetch_accounts,
    state::{PoolDependencies, QuoteState, VersionedAccountData},
    tokens::WSOL,
};

const METEORA_BIN_ARRAY_TAKE_COUNT: u8 = 3;
const ROUND_TRIP_PROBE_LAMPORTS: [u64; 3] = [10_000_000, 50_000_000, 100_000_000];
const COST_FLOOR_BASE_FEE_LAMPORTS: u64 = 5_000;
const COST_FLOOR_JITO_TIP_LAMPORTS: u64 = 1_000;

#[derive(Debug)]
pub struct DynamicOpportunityEvaluation {
    pub snapshot_slot: u64,
    pub route_count: usize,
    pub events: Vec<OpportunityEvent>,
}

pub async fn evaluate_post_event_pools(
    client: &Client,
    config: &HeliusConfig,
    token_mint: &str,
    pools: &[PoolInfo],
    event_slot: u64,
) -> Result<DynamicOpportunityEvaluation> {
    if token_mint.trim().is_empty() || token_mint == WSOL {
        bail!("dynamic quote token mint must be a non-WSOL mint");
    }
    if pools.len() < 2 {
        bail!("dynamic quote requires at least two supported pools");
    }
    for pool in pools {
        if !pool.matches_pair(token_mint, WSOL) {
            bail!(
                "dynamic quote pool is outside event Mint/WSOL pair: {}",
                pool.address
            );
        }
    }

    let (state, snapshot_slot) =
        build_coherent_post_event_state(client, config, pools, event_slot).await?;
    let cache = QuoteContextCache::build(&state, pools)?;
    for pool in pools {
        let slot = coherent_pool_slot(&state, &pool.address)?;
        if slot != snapshot_slot || slot < event_slot {
            bail!(
                "dynamic quote pool snapshot is not coherent with event: pool={} slot={} snapshot={} event={}",
                pool.address,
                slot,
                snapshot_slot,
                event_slot
            );
        }
    }

    let affected = pools
        .iter()
        .map(|pool| pool.address.clone())
        .collect::<Vec<_>>();
    let routes = affected_directed_pool_routes(pools, &affected, WSOL)?;
    if routes.is_empty() {
        bail!("dynamic event produced no two-pool routes");
    }
    if routes.iter().any(|route| route.token_mint != token_mint) {
        bail!("dynamic route token does not match event mint");
    }

    let needs_meteora_clock = routes.iter().any(|route| {
        route.first_pool.dex == Dex::MeteoraDlmm || route.second_pool.dex == Dex::MeteoraDlmm
    });
    let clock = if needs_meteora_clock {
        let address = clock_sysvar_address();
        let batch = fetch_accounts(
            client,
            config.http_url().as_str(),
            std::slice::from_ref(&address),
            Some(snapshot_slot),
        )
        .await?;
        let account = batch.accounts[0]
            .as_ref()
            .context("Clock sysvar missing during dynamic quote")?;
        Some(decode_clock(&account.data)?)
    } else {
        None
    };
    let runtime = QuoteRuntime {
        unix_timestamp: unix_timestamp()?,
        meteora_clock: clock.as_ref(),
    };
    let execution_cost = ExecutionCost {
        base_fee_lamports: COST_FLOOR_BASE_FEE_LAMPORTS,
        jito_tip_lamports: COST_FLOOR_JITO_TIP_LAMPORTS,
        ..ExecutionCost::ZERO
    };

    let mut events = Vec::with_capacity(routes.len() * ROUND_TRIP_PROBE_LAMPORTS.len());
    for route in &routes {
        events.extend(evaluate_route_events(
            &cache,
            route,
            execution_cost,
            runtime,
        )?);
    }
    let expected = routes.len() * ROUND_TRIP_PROBE_LAMPORTS.len();
    if events.len() != expected {
        bail!("dynamic quote did not account for every route probe");
    }

    Ok(DynamicOpportunityEvaluation {
        snapshot_slot,
        route_count: routes.len(),
        events,
    })
}

async fn build_coherent_post_event_state(
    client: &Client,
    config: &HeliusConfig,
    pools: &[PoolInfo],
    event_slot: u64,
) -> Result<(QuoteState, u64)> {
    let mut state = QuoteState::new();
    let mut seen = HashSet::new();
    for pool in pools {
        if !seen.insert(pool.address.as_str()) {
            bail!("duplicate pool in dynamic event universe: {}", pool.address);
        }
        state.replace_pool_dependencies(
            build_pool_dependencies(client, config, pool, Some(event_slot)).await?,
        )?;
    }

    preload_registered_accounts(client, config, &mut state, Some(event_slot)).await?;

    // Pool State may have crossed a Tick/Bin boundary while dependencies were being built.
    // Re-derive once, then load the resulting complete dependency set before the final snapshot.
    for pool in pools {
        state.replace_pool_dependencies(
            build_pool_dependencies(client, config, pool, Some(event_slot)).await?,
        )?;
    }
    preload_registered_accounts(client, config, &mut state, Some(event_slot)).await?;

    let coherence_floor = max_registered_slot(&state)?.max(event_slot);
    let snapshot_slot =
        preload_registered_accounts(client, config, &mut state, Some(coherence_floor)).await?;
    if snapshot_slot < coherence_floor {
        bail!("final dynamic quote snapshot did not reach coherence floor");
    }

    // Fail closed if the final Pool State implies a different Tick/Bin dependency set.
    for pool in pools {
        let verified = build_pool_dependencies(client, config, pool, Some(snapshot_slot)).await?;
        let current = state
            .dependencies_for_pool(&pool.address)
            .context("dynamic pool dependencies disappeared")?;
        if current.accounts != verified.accounts {
            bail!(
                "dynamic dependency set changed during coherent snapshot: {}",
                pool.address
            );
        }
        if coherent_pool_slot(&state, &pool.address)? != snapshot_slot {
            bail!("dynamic pool dependencies are not from one RPC context slot");
        }
    }

    Ok((state, snapshot_slot))
}

async fn preload_registered_accounts(
    client: &Client,
    config: &HeliusConfig,
    state: &mut QuoteState,
    min_context_slot: Option<u64>,
) -> Result<u64> {
    let addresses = state.unique_dependency_addresses();
    if addresses.is_empty() {
        bail!("dynamic quote dependency set is empty");
    }
    let batch = fetch_accounts(
        client,
        config.http_url().as_str(),
        &addresses,
        min_context_slot,
    )
    .await?;
    if batch.accounts.len() != addresses.len() {
        bail!("dynamic dependency snapshot account count mismatch");
    }
    for (address, account) in addresses.iter().zip(batch.accounts) {
        let account = account
            .with_context(|| format!("dynamic quote dependency account missing: {address}"))?;
        let applied = state.apply_account_update(
            address,
            VersionedAccountData {
                slot: batch.slot,
                owner: account.owner,
                data: account.data,
            },
        )?;
        if !applied.accepted {
            bail!("dynamic coherent snapshot attempted to move an account backward");
        }
    }
    Ok(batch.slot)
}

fn max_registered_slot(state: &QuoteState) -> Result<u64> {
    let mut max_slot = None;
    for address in state.unique_dependency_addresses() {
        let slot = state
            .account_data(&address)
            .with_context(|| format!("dynamic dependency has no loaded data: {address}"))?
            .slot;
        max_slot = Some(max_slot.map_or(slot, |current: u64| current.max(slot)));
    }
    max_slot.context("dynamic dependency set has no loaded slots")
}

fn coherent_pool_slot(state: &QuoteState, pool_address: &str) -> Result<u64> {
    let dependencies = state
        .dependencies_for_pool(pool_address)
        .with_context(|| format!("unknown dynamic pool: {pool_address}"))?;
    let mut slot = None;
    for dependency in &dependencies.accounts {
        let current = state
            .account_data(&dependency.address)
            .with_context(|| format!("missing dynamic dependency data: {}", dependency.address))?
            .slot;
        match slot {
            None => slot = Some(current),
            Some(expected) if expected == current => {}
            Some(expected) => bail!(
                "mixed dependency slots inside dynamic pool {}: {} vs {}",
                pool_address,
                expected,
                current
            ),
        }
    }
    slot.context("dynamic pool has no dependency slots")
}

async fn build_pool_dependencies(
    client: &Client,
    config: &HeliusConfig,
    pool: &PoolInfo,
    min_context_slot: Option<u64>,
) -> Result<PoolDependencies> {
    match pool.dex {
        Dex::Raydium => {
            if pool.pool_type != "Standard"
                || pool.program_id.as_deref() != Some(RAYDIUM_AMM_V4_PROGRAM_ID)
            {
                bail!("unsupported dynamic Raydium pool: {}", pool.address);
            }
            let batch = fetch_accounts(
                client,
                config.http_url().as_str(),
                std::slice::from_ref(&pool.address),
                min_context_slot,
            )
            .await?;
            let account = batch.accounts[0]
                .as_ref()
                .context("dynamic Raydium Pool State missing")?;
            if account.owner != RAYDIUM_AMM_V4_PROGRAM_ID {
                bail!("dynamic Raydium Pool State owner mismatch");
            }
            let decoded = decode_amm_v4(&account.data)?;
            if !pool.matches_pair(&decoded.coin_mint, &decoded.pc_mint) {
                bail!("dynamic Raydium decoded pair does not match discovery metadata");
            }
            raydium_standard_dependencies(pool, &decoded)
        }
        Dex::Orca => {
            let program_id = Pubkey::from_str(ORCA_WHIRLPOOL_PROGRAM_ID)
                .context("invalid Orca Whirlpool program id")?;
            let batch = fetch_accounts(
                client,
                config.http_url().as_str(),
                std::slice::from_ref(&pool.address),
                min_context_slot,
            )
            .await?;
            let account = batch.accounts[0]
                .as_ref()
                .context("dynamic Orca Whirlpool missing")?;
            if account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
                bail!("dynamic Orca Whirlpool owner mismatch");
            }
            let whirlpool = decode_whirlpool(&account.data)?;
            let mint_a = whirlpool.token_mint_a.to_string();
            let mint_b = whirlpool.token_mint_b.to_string();
            if !pool.matches_pair(&mint_a, &mint_b) {
                bail!("dynamic Orca decoded pair does not match discovery metadata");
            }
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
                        bail!("dynamic Orca TickArray owner mismatch: {address}");
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
            let batch = fetch_accounts(
                client,
                config.http_url().as_str(),
                &addresses,
                min_context_slot,
            )
            .await?;
            let account = batch.accounts[0]
                .as_ref()
                .context("dynamic Meteora LbPair missing")?;
            if account.owner != DLMM_PROGRAM_ID {
                bail!("dynamic Meteora LbPair owner mismatch");
            }
            let lb_pair = decode_lb_pair(&account.data)?;
            let mint_x = lb_pair.token_x_mint.to_string();
            let mint_y = lb_pair.token_y_mint.to_string();
            if !pool.matches_pair(&mint_x, &mint_y) {
                bail!("dynamic Meteora decoded pair does not match discovery metadata");
            }
            let bitmap = batch.accounts[1]
                .as_ref()
                .map(|account| {
                    if account.owner != DLMM_PROGRAM_ID {
                        bail!("dynamic Meteora bitmap owner mismatch");
                    }
                    decode_bitmap_extension(&account.data)
                })
                .transpose()?;
            let swap_for_y = swap_for_y_for_input(&lb_pair, WSOL)?;
            let mut bins = bin_array_addresses_for_swap(
                &pool.address,
                &lb_pair,
                bitmap.as_ref(),
                swap_for_y,
                METEORA_BIN_ARRAY_TAKE_COUNT,
            )?;
            bins.extend(bin_array_addresses_for_swap(
                &pool.address,
                &lb_pair,
                bitmap.as_ref(),
                !swap_for_y,
                METEORA_BIN_ARRAY_TAKE_COUNT,
            )?);
            bins.sort();
            bins.dedup();
            meteora_dlmm_dependencies(
                pool,
                &lb_pair,
                &bins,
                batch.accounts[1].as_ref().map(|_| bitmap_address.as_str()),
            )
        }
        Dex::MeteoraDammV2 => bail!("Meteora DAMM v2 is not dynamically quoteable"),
    }
}

fn evaluate_route_events(
    cache: &QuoteContextCache,
    route: &DirectedPoolRoute,
    execution_cost: ExecutionCost,
    runtime: QuoteRuntime<'_>,
) -> Result<Vec<OpportunityEvent>> {
    let first_points = cache.quote_many(
        &route.first_pool.address,
        WSOL,
        &ROUND_TRIP_PROBE_LAMPORTS,
        runtime,
    )?;
    let mut events = Vec::with_capacity(ROUND_TRIP_PROBE_LAMPORTS.len());
    let mut available_first: Vec<(usize, SwapQuote)> = Vec::new();
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
        let second_points = cache.quote_many(
            &route.second_pool.address,
            &route.token_mint,
            &second_inputs,
            runtime,
        )?;
        if second_points.len() != available_first.len() {
            bail!("dynamic second-leg quote count mismatch");
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
        bail!("dynamic route did not account for every probe amount");
    }
    Ok(events)
}

fn unix_timestamp() -> Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before Unix epoch")?
        .as_secs()
        .try_into()
        .context("Unix timestamp does not fit u64")
}
