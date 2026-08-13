use anyhow::Result;
use commons::dlmm::accounts::LbPair;

use crate::{
    dex::raydium_amm::RaydiumAmmV4State,
    model::PoolInfo,
    state::{DependencyAccount, DependencyKind, PoolDependencies},
};

pub fn raydium_standard_dependencies(
    pool: &PoolInfo,
    state: &RaydiumAmmV4State,
) -> Result<PoolDependencies> {
    PoolDependencies::new(
        pool.clone(),
        vec![
            DependencyAccount::new(&pool.address, DependencyKind::PoolState)?,
            DependencyAccount::new(&state.coin_vault, DependencyKind::TokenVault)?,
            DependencyAccount::new(&state.pc_vault, DependencyKind::TokenVault)?,
        ],
    )
}

pub fn orca_whirlpool_dependencies(
    pool: &PoolInfo,
    existing_tick_arrays: &[String],
    oracle_address: Option<&str>,
) -> Result<PoolDependencies> {
    let mut accounts = Vec::with_capacity(2 + existing_tick_arrays.len());
    accounts.push(DependencyAccount::new(
        &pool.address,
        DependencyKind::PoolState,
    )?);
    accounts.extend(
        existing_tick_arrays
            .iter()
            .map(|address| DependencyAccount::new(address, DependencyKind::TickArray))
            .collect::<Result<Vec<_>>>()?,
    );
    if let Some(address) = oracle_address {
        accounts.push(DependencyAccount::new(address, DependencyKind::Oracle)?);
    }

    // 当前 BONK/WIF Orca 池已实证为经典 SPL Token，官方 quote 不需要 Mint 状态参与数学。
    // 若未来支持 Token-2022 transfer fee，这里必须把 token_mint_a / token_mint_b 加入依赖。
    PoolDependencies::new(pool.clone(), accounts)
}

pub fn meteora_dlmm_dependencies(
    pool: &PoolInfo,
    lb_pair: &LbPair,
    bin_arrays: &[String],
    bitmap_extension_address: Option<&str>,
) -> Result<PoolDependencies> {
    let mut accounts = Vec::with_capacity(4 + bin_arrays.len());
    accounts.push(DependencyAccount::new(
        &pool.address,
        DependencyKind::PoolState,
    )?);
    accounts.push(DependencyAccount::new(
        lb_pair.token_x_mint.to_string(),
        DependencyKind::TokenMint,
    )?);
    accounts.push(DependencyAccount::new(
        lb_pair.token_y_mint.to_string(),
        DependencyKind::TokenMint,
    )?);
    if let Some(address) = bitmap_extension_address {
        accounts.push(DependencyAccount::new(
            address,
            DependencyKind::BitmapExtension,
        )?);
    }
    accounts.extend(
        bin_arrays
            .iter()
            .map(|address| DependencyAccount::new(address, DependencyKind::BinArray))
            .collect::<Result<Vec<_>>>()?,
    );

    // Clock 每个 slot 都变化，若把 Clock sysvar 加进 WSS 依赖会让所有 DLMM 池每个 slot 都被触发。
    // 生产重算时只在真正的 DLMM 依赖账户变化后刷新 Clock，而不订阅 Clock 本身。
    PoolDependencies::new(pool.clone(), accounts)
}

#[cfg(test)]
mod tests {
    use anchor_client::solana_sdk::pubkey::Pubkey;
    use anchor_lang::Discriminator;
    use commons::dlmm::accounts::LbPair;
    use std::mem::size_of;

    use super::*;
    use crate::{
        dex::meteora_dlmm::decode_lb_pair,
        model::Dex,
        state::DependencyKind,
    };

    fn pool(dex: Dex, address: &str) -> PoolInfo {
        PoolInfo {
            dex,
            address: address.into(),
            pool_type: "test".into(),
            program_id: Some("program".into()),
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd: 1_000.0,
        }
    }

    #[test]
    fn raydium_dependencies_include_pool_and_both_vaults() {
        let state = RaydiumAmmV4State {
            status: 6,
            coin_decimals: 9,
            pc_decimals: 5,
            swap_fee_numerator: 25,
            swap_fee_denominator: 10_000,
            need_take_pnl_coin: 0,
            need_take_pnl_pc: 0,
            pool_open_time: 0,
            coin_vault: "coin-vault".into(),
            pc_vault: "pc-vault".into(),
            coin_mint: "coin".into(),
            pc_mint: "pc".into(),
        };
        let dependencies =
            raydium_standard_dependencies(&pool(Dex::Raydium, "pool-a"), &state).unwrap();

        assert_eq!(dependencies.accounts.len(), 3);
        assert!(dependencies.accounts.iter().any(|account| {
            account.address == "coin-vault" && account.kind == DependencyKind::TokenVault
        }));
        assert!(dependencies.accounts.iter().any(|account| {
            account.address == "pc-vault" && account.kind == DependencyKind::TokenVault
        }));
    }

    #[test]
    fn orca_dependencies_only_include_existing_tick_arrays_and_optional_oracle() {
        let ticks = vec!["tick-a".to_owned(), "tick-b".to_owned()];
        let dependencies = orca_whirlpool_dependencies(
            &pool(Dex::Orca, "pool-a"),
            &ticks,
            Some("oracle"),
        )
        .unwrap();

        assert_eq!(dependencies.accounts.len(), 4);
        assert!(dependencies.accounts.iter().any(|account| {
            account.address == "oracle" && account.kind == DependencyKind::Oracle
        }));
        assert!(!dependencies
            .accounts
            .iter()
            .any(|account| account.address == "missing-tick"));
    }

    #[test]
    fn meteora_dependencies_include_mints_bins_and_optional_bitmap_but_not_clock() {
        let mut data = vec![0u8; 8 + size_of::<LbPair>()];
        data[..LbPair::DISCRIMINATOR.len()].copy_from_slice(LbPair::DISCRIMINATOR);
        let mut lb_pair = decode_lb_pair(&data).unwrap();
        lb_pair.token_x_mint = Pubkey::new_from_array([1u8; 32]);
        lb_pair.token_y_mint = Pubkey::new_from_array([2u8; 32]);
        let bins = vec!["bin-a".to_owned(), "bin-b".to_owned()];
        let dependencies = meteora_dlmm_dependencies(
            &pool(Dex::MeteoraDlmm, "pool-a"),
            &lb_pair,
            &bins,
            Some("bitmap"),
        )
        .unwrap();

        assert_eq!(dependencies.accounts.len(), 6);
        assert_eq!(
            dependencies
                .accounts
                .iter()
                .filter(|account| account.kind == DependencyKind::TokenMint)
                .count(),
            2
        );
        assert_eq!(
            dependencies
                .accounts
                .iter()
                .filter(|account| account.kind == DependencyKind::BinArray)
                .count(),
            2
        );
        assert!(!dependencies
            .accounts
            .iter()
            .any(|account| account.address == crate::dex::meteora_dlmm::clock_sysvar_address()));
    }
}
