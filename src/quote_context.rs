use std::{collections::HashMap, str::FromStr};

use anchor_client::solana_sdk::{account::Account, clock::Clock, pubkey::Pubkey as MeteoraPubkey};
use anyhow::{bail, Context, Result};
use commons::dlmm::accounts::{BinArray, BinArrayBitmapExtension, LbPair};
use orca_whirlpools_client::{get_oracle_address, get_tick_array_address, Whirlpool};
use orca_whirlpools_core::{OracleFacade, TickArrayFacade};
use solana_pubkey::Pubkey as OrcaPubkey;

use crate::{
    dex::{
        meteora::DLMM_PROGRAM_ID,
        meteora_dlmm::{
            build_bin_array_map, decode_bin_array, decode_bitmap_extension, decode_lb_pair,
            is_pool_out_of_liquidity as is_meteora_pool_out_of_liquidity,
            quote_exact_in as quote_meteora_exact_in, quote_mint_account, swap_for_y_for_input,
        },
        orca_whirlpool::{
            decode_oracle, decode_tick_array_or_default, decode_whirlpool, needs_oracle,
            quote_exact_in as quote_orca_exact_in, tick_array_start_indexes,
            ORCA_WHIRLPOOL_PROGRAM_ID,
        },
        raydium_amm::{decode_amm_v4, quote_base_in, RaydiumAmmV4State, RAYDIUM_AMM_V4_PROGRAM_ID},
    },
    model::{Dex, PoolInfo},
    opportunity::SwapQuote,
    state::{DependencyKind, QuoteState},
    token_account::{decode_spl_token_account, SPL_TOKEN_PROGRAM_ID},
};

#[derive(Clone, Copy)]
pub struct QuoteRuntime<'a> {
    pub unix_timestamp: u64,
    pub meteora_clock: Option<&'a Clock>,
}

pub struct QuoteContextCache {
    contexts: HashMap<String, PoolQuoteContext>,
}

impl QuoteContextCache {
    pub fn build(state: &QuoteState, pools: &[PoolInfo]) -> Result<Self> {
        let mut cache = Self {
            contexts: HashMap::with_capacity(pools.len()),
        };
        for pool in pools {
            cache.insert_context(build_pool_quote_context(state, pool)?)?;
        }
        Ok(cache)
    }

    pub fn refresh_pool(&mut self, state: &QuoteState, pool: &PoolInfo) -> Result<()> {
        self.insert_context(build_pool_quote_context(state, pool)?)
    }

    pub fn len(&self) -> usize {
        self.contexts.len()
    }

    pub fn snapshot_slot(&self, pool_address: &str) -> Result<u64> {
        Ok(self.context(pool_address)?.snapshot_slot())
    }

    pub fn quote_many(
        &self,
        pool_address: &str,
        input_mint: &str,
        amounts_in: &[u64],
        runtime: QuoteRuntime<'_>,
    ) -> Result<Vec<Option<SwapQuote>>> {
        self.context(pool_address)?
            .quote_many(input_mint, amounts_in, runtime)
    }

    fn context(&self, pool_address: &str) -> Result<&PoolQuoteContext> {
        self.contexts
            .get(pool_address)
            .with_context(|| format!("quote context is missing for pool: {pool_address}"))
    }

    fn insert_context(&mut self, context: PoolQuoteContext) -> Result<()> {
        let pool_address = context.pool().address.clone();
        if let Some(current) = self.contexts.get(&pool_address) {
            if context.snapshot_slot() < current.snapshot_slot() {
                bail!(
                    "quote context slot would move backward for {}: {} < {}",
                    pool_address,
                    context.snapshot_slot(),
                    current.snapshot_slot()
                );
            }
        }
        self.contexts.insert(pool_address, context);
        Ok(())
    }
}

enum PoolQuoteContext {
    Raydium(Box<RaydiumQuoteContext>),
    Orca(Box<OrcaQuoteContext>),
    Meteora(Box<MeteoraQuoteContext>),
}

impl PoolQuoteContext {
    fn pool(&self) -> &PoolInfo {
        match self {
            Self::Raydium(context) => &context.pool,
            Self::Orca(context) => &context.pool,
            Self::Meteora(context) => &context.pool,
        }
    }

    fn snapshot_slot(&self) -> u64 {
        match self {
            Self::Raydium(context) => context.snapshot_slot,
            Self::Orca(context) => context.snapshot_slot,
            Self::Meteora(context) => context.snapshot_slot,
        }
    }

    fn quote_many(
        &self,
        input_mint: &str,
        amounts_in: &[u64],
        runtime: QuoteRuntime<'_>,
    ) -> Result<Vec<Option<SwapQuote>>> {
        if amounts_in.is_empty() {
            bail!("local quote context batch must contain at least one amount");
        }
        match self {
            Self::Raydium(context) => {
                context.quote_many(input_mint, amounts_in, runtime.unix_timestamp)
            }
            Self::Orca(context) => {
                context.quote_many(input_mint, amounts_in, runtime.unix_timestamp)
            }
            Self::Meteora(context) => context.quote_many(
                input_mint,
                amounts_in,
                runtime
                    .meteora_clock
                    .context("Meteora local quote requires refreshed Clock sysvar")?,
            ),
        }
    }
}

struct RaydiumQuoteContext {
    pool: PoolInfo,
    state: RaydiumAmmV4State,
    coin_vault_amount: u64,
    pc_vault_amount: u64,
    snapshot_slot: u64,
}

impl RaydiumQuoteContext {
    fn quote_many(
        &self,
        input_mint: &str,
        amounts_in: &[u64],
        unix_timestamp: u64,
    ) -> Result<Vec<Option<SwapQuote>>> {
        amounts_in
            .iter()
            .map(|&amount_in| {
                let quote = quote_base_in(
                    &self.state,
                    self.coin_vault_amount,
                    self.pc_vault_amount,
                    input_mint,
                    amount_in,
                    unix_timestamp,
                )?;
                Ok(Some(SwapQuote::new(
                    self.pool.dex,
                    self.pool.address.clone(),
                    quote.input_mint,
                    quote.output_mint,
                    quote.amount_in,
                    quote.amount_out,
                    self.snapshot_slot,
                )?))
            })
            .collect()
    }
}

struct OrcaQuoteContext {
    pool: PoolInfo,
    whirlpool: Whirlpool,
    tick_arrays: [TickArrayFacade; 5],
    oracle: Option<OracleFacade>,
    snapshot_slot: u64,
}

impl OrcaQuoteContext {
    fn quote_many(
        &self,
        input_mint: &str,
        amounts_in: &[u64],
        unix_timestamp: u64,
    ) -> Result<Vec<Option<SwapQuote>>> {
        let input = OrcaPubkey::from_str(input_mint).context("invalid Orca input mint")?;
        let output_mint = if input == self.whirlpool.token_mint_a {
            self.whirlpool.token_mint_b.to_string()
        } else if input == self.whirlpool.token_mint_b {
            self.whirlpool.token_mint_a.to_string()
        } else {
            bail!("Orca local context input mint is outside pool");
        };

        amounts_in
            .iter()
            .map(|&amount_in| {
                let quote = quote_orca_exact_in(
                    &self.whirlpool,
                    self.tick_arrays,
                    self.oracle,
                    input_mint,
                    amount_in,
                    unix_timestamp,
                )?;
                if quote.token_est_out == 0 {
                    bail!("Orca local context quote returned zero output");
                }
                Ok(Some(SwapQuote::new(
                    self.pool.dex,
                    self.pool.address.clone(),
                    input_mint,
                    output_mint.clone(),
                    amount_in,
                    quote.token_est_out,
                    self.snapshot_slot,
                )?))
            })
            .collect()
    }
}

struct MeteoraQuoteContext {
    pool: PoolInfo,
    lb_pair: LbPair,
    bitmap: Option<BinArrayBitmapExtension>,
    bin_arrays: HashMap<MeteoraPubkey, BinArray>,
    mint_x_account: Account,
    mint_y_account: Account,
    snapshot_slot: u64,
}

impl MeteoraQuoteContext {
    fn quote_many(
        &self,
        input_mint: &str,
        amounts_in: &[u64],
        clock: &Clock,
    ) -> Result<Vec<Option<SwapQuote>>> {
        let swap_for_y = swap_for_y_for_input(&self.lb_pair, input_mint)?;
        let output_mint = if swap_for_y {
            self.lb_pair.token_y_mint.to_string()
        } else {
            self.lb_pair.token_x_mint.to_string()
        };

        let mut results = Vec::with_capacity(amounts_in.len());
        for &amount_in in amounts_in {
            let quote = match quote_meteora_exact_in(
                &self.pool.address,
                &self.lb_pair,
                amount_in,
                swap_for_y,
                self.bin_arrays.clone(),
                self.bitmap.as_ref(),
                clock,
                &self.mint_x_account,
                &self.mint_y_account,
            ) {
                Ok(quote) => quote,
                Err(error) if is_meteora_pool_out_of_liquidity(&error) => {
                    results.push(None);
                    continue;
                }
                Err(error) => return Err(error),
            };
            if quote.amount_out == 0 {
                bail!("Meteora local context quote returned zero output");
            }
            results.push(Some(SwapQuote::new(
                self.pool.dex,
                self.pool.address.clone(),
                input_mint,
                output_mint.clone(),
                amount_in,
                quote.amount_out,
                self.snapshot_slot,
            )?));
        }
        Ok(results)
    }
}

fn build_pool_quote_context(state: &QuoteState, pool: &PoolInfo) -> Result<PoolQuoteContext> {
    let missing = state.missing_accounts_for_pool(&pool.address)?;
    if !missing.is_empty() {
        bail!(
            "cannot build local quote context with missing dependencies for {}: {missing:?}",
            pool.address
        );
    }
    let snapshot_slot = newest_dependency_slot(state, pool)?;

    match pool.dex {
        Dex::Raydium => build_raydium_context(state, pool, snapshot_slot),
        Dex::Orca => build_orca_context(state, pool, snapshot_slot),
        Dex::MeteoraDlmm => build_meteora_context(state, pool, snapshot_slot),
        Dex::MeteoraDammV2 => bail!("Meteora DAMM v2 has no V3.5 local quote context"),
    }
}

fn newest_dependency_slot(state: &QuoteState, pool: &PoolInfo) -> Result<u64> {
    let dependencies = state
        .dependencies_for_pool(&pool.address)
        .with_context(|| format!("missing dependency metadata for pool: {}", pool.address))?;
    dependencies
        .accounts
        .iter()
        .map(|dependency| {
            state
                .account_data(&dependency.address)
                .map(|account| account.slot)
                .with_context(|| {
                    format!(
                        "local quote dependency has no account data: {}",
                        dependency.address
                    )
                })
        })
        .collect::<Result<Vec<_>>>()?
        .into_iter()
        .max()
        .context("pool has no local quote dependency slots")
}

fn build_raydium_context(
    state: &QuoteState,
    pool: &PoolInfo,
    snapshot_slot: u64,
) -> Result<PoolQuoteContext> {
    let pool_account = state
        .account_data(&pool.address)
        .context("Raydium local Pool State missing")?;
    if pool_account.owner != RAYDIUM_AMM_V4_PROGRAM_ID {
        bail!("Raydium local Pool State owner mismatch");
    }
    let amm = decode_amm_v4(&pool_account.data)?;
    if !pool.matches_pair(&amm.coin_mint, &amm.pc_mint) {
        bail!("Raydium local context mints do not match discovery metadata");
    }
    let coin_vault_data = state
        .account_data(&amm.coin_vault)
        .context("Raydium local coin vault missing")?;
    let pc_vault_data = state
        .account_data(&amm.pc_vault)
        .context("Raydium local pc vault missing")?;
    if coin_vault_data.owner != SPL_TOKEN_PROGRAM_ID || pc_vault_data.owner != SPL_TOKEN_PROGRAM_ID
    {
        bail!("Raydium local vault owner mismatch");
    }
    let coin_vault = decode_spl_token_account(&coin_vault_data.data)?;
    let pc_vault = decode_spl_token_account(&pc_vault_data.data)?;
    if coin_vault.mint != amm.coin_mint || pc_vault.mint != amm.pc_mint {
        bail!("Raydium local vault mint mismatch");
    }

    Ok(PoolQuoteContext::Raydium(Box::new(RaydiumQuoteContext {
        pool: pool.clone(),
        state: amm,
        coin_vault_amount: coin_vault.amount,
        pc_vault_amount: pc_vault.amount,
        snapshot_slot,
    })))
}

fn build_orca_context(
    state: &QuoteState,
    pool: &PoolInfo,
    snapshot_slot: u64,
) -> Result<PoolQuoteContext> {
    let pool_account = state
        .account_data(&pool.address)
        .context("Orca local Whirlpool missing")?;
    if pool_account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
        bail!("Orca local Whirlpool owner mismatch");
    }
    let whirlpool = decode_whirlpool(&pool_account.data)?;
    let mint_a = whirlpool.token_mint_a.to_string();
    let mint_b = whirlpool.token_mint_b.to_string();
    if !pool.matches_pair(&mint_a, &mint_b) {
        bail!("Orca local context mints do not match discovery metadata");
    }

    let program_id =
        OrcaPubkey::from_str(ORCA_WHIRLPOOL_PROGRAM_ID).context("invalid Orca program id")?;
    let pool_pubkey = OrcaPubkey::from_str(&pool.address).context("invalid Orca pool address")?;
    let tick_indexes =
        tick_array_start_indexes(whirlpool.tick_current_index, whirlpool.tick_spacing);
    let mut tick_facades = Vec::with_capacity(5);
    for index in tick_indexes {
        let address = get_tick_array_address(&pool_pubkey, index, Some(program_id))
            .map_err(|error| anyhow::anyhow!("failed to derive Orca TickArray PDA: {error}"))?
            .0
            .to_string();
        if let Some(account) = state.account_data(&address) {
            if account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
                bail!("Orca local TickArray owner mismatch: {address}");
            }
            tick_facades.push(decode_tick_array_or_default(Some(&account.data), index)?);
        } else {
            tick_facades.push(decode_tick_array_or_default(None, index)?);
        }
    }
    let tick_arrays: [TickArrayFacade; 5] = tick_facades
        .try_into()
        .map_err(|_| anyhow::anyhow!("Orca local context did not produce five TickArrays"))?;

    let oracle = if needs_oracle(&whirlpool) {
        let address = get_oracle_address(&pool_pubkey, Some(program_id))
            .map_err(|error| anyhow::anyhow!("failed to derive Orca Oracle PDA: {error}"))?
            .0
            .to_string();
        let account = state
            .account_data(&address)
            .context("Orca local adaptive-fee Oracle missing")?;
        if account.owner != ORCA_WHIRLPOOL_PROGRAM_ID {
            bail!("Orca local Oracle owner mismatch");
        }
        let oracle = decode_oracle(&account.data)?;
        if oracle.whirlpool != pool_pubkey {
            bail!("Orca local Oracle points to another Whirlpool");
        }
        Some(oracle.into())
    } else {
        None
    };

    Ok(PoolQuoteContext::Orca(Box::new(OrcaQuoteContext {
        pool: pool.clone(),
        whirlpool,
        tick_arrays,
        oracle,
        snapshot_slot,
    })))
}

fn build_meteora_context(
    state: &QuoteState,
    pool: &PoolInfo,
    snapshot_slot: u64,
) -> Result<PoolQuoteContext> {
    let dependencies = state
        .dependencies_for_pool(&pool.address)
        .context("Meteora local dependency metadata missing")?;
    let pool_account = state
        .account_data(&pool.address)
        .context("Meteora local LbPair missing")?;
    if pool_account.owner != DLMM_PROGRAM_ID {
        bail!("Meteora local LbPair owner mismatch");
    }
    let lb_pair = decode_lb_pair(&pool_account.data)?;
    let mint_x = lb_pair.token_x_mint.to_string();
    let mint_y = lb_pair.token_y_mint.to_string();
    if !pool.matches_pair(&mint_x, &mint_y) {
        bail!("Meteora local context mints do not match discovery metadata");
    }

    let bitmap = dependencies
        .accounts
        .iter()
        .find(|dependency| dependency.kind == DependencyKind::BitmapExtension)
        .map(|dependency| {
            let account = state
                .account_data(&dependency.address)
                .context("Meteora local bitmap extension missing")?;
            if account.owner != DLMM_PROGRAM_ID {
                bail!("Meteora local bitmap extension owner mismatch");
            }
            decode_bitmap_extension(&account.data)
        })
        .transpose()?;

    let bin_entries = dependencies
        .accounts
        .iter()
        .filter(|dependency| dependency.kind == DependencyKind::BinArray)
        .map(|dependency| {
            let account = state.account_data(&dependency.address).with_context(|| {
                format!("Meteora local BinArray missing: {}", dependency.address)
            })?;
            if account.owner != DLMM_PROGRAM_ID {
                bail!(
                    "Meteora local BinArray owner mismatch: {}",
                    dependency.address
                );
            }
            Ok((dependency.address.clone(), decode_bin_array(&account.data)?))
        })
        .collect::<Result<Vec<_>>>()?;
    if bin_entries.is_empty() {
        bail!("Meteora local context has no BinArray dependencies");
    }

    let mint_x_data = state
        .account_data(&mint_x)
        .context("Meteora local token X mint missing")?;
    let mint_y_data = state
        .account_data(&mint_y)
        .context("Meteora local token Y mint missing")?;

    Ok(PoolQuoteContext::Meteora(Box::new(MeteoraQuoteContext {
        pool: pool.clone(),
        lb_pair,
        bitmap,
        bin_arrays: build_bin_array_map(bin_entries)?,
        mint_x_account: quote_mint_account(&mint_x_data.owner, &mint_x_data.data)?,
        mint_y_account: quote_mint_account(&mint_y_data.owner, &mint_y_data.data)?,
        snapshot_slot,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raydium_pool(address: &str) -> PoolInfo {
        PoolInfo {
            dex: Dex::Raydium,
            address: address.into(),
            pool_type: "Standard".into(),
            program_id: Some(RAYDIUM_AMM_V4_PROGRAM_ID.into()),
            mint_a: "coin".into(),
            mint_b: "pc".into(),
            tvl_usd: 1_000.0,
        }
    }

    fn raydium_context(address: &str, slot: u64) -> PoolQuoteContext {
        PoolQuoteContext::Raydium(Box::new(RaydiumQuoteContext {
            pool: raydium_pool(address),
            state: RaydiumAmmV4State {
                status: 6,
                coin_decimals: 9,
                pc_decimals: 6,
                swap_fee_numerator: 25,
                swap_fee_denominator: 10_000,
                need_take_pnl_coin: 0,
                need_take_pnl_pc: 0,
                pool_open_time: 0,
                coin_vault: "coin-vault".into(),
                pc_vault: "pc-vault".into(),
                coin_mint: "coin".into(),
                pc_mint: "pc".into(),
            },
            coin_vault_amount: 1_000_000_000,
            pc_vault_amount: 2_000_000_000,
            snapshot_slot: slot,
        }))
    }

    #[test]
    fn cache_rejects_context_slot_regression() {
        let mut cache = QuoteContextCache {
            contexts: HashMap::new(),
        };
        cache.insert_context(raydium_context("pool", 10)).unwrap();
        assert!(cache.insert_context(raydium_context("pool", 9)).is_err());
        cache.insert_context(raydium_context("pool", 10)).unwrap();
        cache.insert_context(raydium_context("pool", 11)).unwrap();
        assert_eq!(cache.snapshot_slot("pool").unwrap(), 11);
    }

    #[test]
    fn cached_raydium_context_quotes_multiple_amounts_without_rpc() {
        let mut cache = QuoteContextCache {
            contexts: HashMap::new(),
        };
        cache.insert_context(raydium_context("pool", 7)).unwrap();
        let quotes = cache
            .quote_many(
                "pool",
                "coin",
                &[1_000, 10_000],
                QuoteRuntime {
                    unix_timestamp: 1,
                    meteora_clock: None,
                },
            )
            .unwrap();
        assert_eq!(quotes.len(), 2);
        assert!(quotes.iter().all(Option::is_some));
        assert_eq!(quotes[0].as_ref().unwrap().snapshot_slot, 7);
        assert!(quotes[1].as_ref().unwrap().amount_out > quotes[0].as_ref().unwrap().amount_out);
    }

    #[test]
    fn cache_rejects_unknown_pool_and_empty_batch() {
        let mut cache = QuoteContextCache {
            contexts: HashMap::new(),
        };
        cache.insert_context(raydium_context("pool", 1)).unwrap();
        let runtime = QuoteRuntime {
            unix_timestamp: 1,
            meteora_clock: None,
        };
        assert!(cache.quote_many("missing", "coin", &[1], runtime).is_err());
        assert!(cache.quote_many("pool", "coin", &[], runtime).is_err());
    }
}
