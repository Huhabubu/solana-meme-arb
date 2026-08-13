use std::str::FromStr;

use anyhow::{bail, Context, Result};
use orca_whirlpools_client::{
    Oracle, TickArray, Whirlpool, ORACLE_DISCRIMINATOR, WHIRLPOOL_DISCRIMINATOR,
};
use orca_whirlpools_core::{
    get_tick_array_start_tick_index, swap_quote_by_input_token, ExactInSwapQuote, OracleFacade,
    TickArrayFacade, TickFacade, TICK_ARRAY_SIZE,
};
use solana_pubkey::Pubkey;

pub const ORCA_WHIRLPOOL_PROGRAM_ID: &str = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc";

/// 使用 Orca 官方生成的 Borsh 解码器解析 Whirlpool Account，并先校验长度与 discriminator。
pub fn decode_whirlpool(data: &[u8]) -> Result<Whirlpool> {
    if data.len() != Whirlpool::LEN {
        bail!(
            "Orca Whirlpool account length mismatch: expected {}, got {}",
            Whirlpool::LEN,
            data.len()
        );
    }
    if data.get(..8) != Some(WHIRLPOOL_DISCRIMINATOR.as_slice()) {
        bail!("Orca Whirlpool discriminator mismatch");
    }

    Whirlpool::from_bytes(data).context("failed to decode Orca Whirlpool account")
}

/// Adaptive Fee 池的 Oracle 也使用 Orca 官方生成的账户解码器。
pub fn decode_oracle(data: &[u8]) -> Result<Oracle> {
    if data.len() != Oracle::LEN {
        bail!(
            "Orca Oracle account length mismatch: expected {}, got {}",
            Oracle::LEN,
            data.len()
        );
    }
    if data.get(..8) != Some(ORACLE_DISCRIMINATOR.as_slice()) {
        bail!("Orca Oracle discriminator mismatch");
    }

    Oracle::from_bytes(data).context("failed to decode Orca Oracle account")
}

/// Orca 官方高层 SDK 为一个 swap 同时准备当前 TickArray、向上两个和向下两个，共 5 个数组。
pub fn tick_array_start_indexes(tick_current_index: i32, tick_spacing: u16) -> [i32; 5] {
    let current = get_tick_array_start_tick_index(tick_current_index, tick_spacing);
    let offset = i32::from(tick_spacing) * TICK_ARRAY_SIZE as i32;
    [
        current,
        current + offset,
        current + offset * 2,
        current - offset,
        current - offset * 2,
    ]
}

/// 未初始化的 TickArray 在 Orca 官方 SDK 中按“全部 tick 未初始化”的空数组参与报价。
pub fn decode_tick_array_or_default(
    data: Option<&[u8]>,
    expected_start_tick_index: i32,
) -> Result<TickArrayFacade> {
    let Some(data) = data else {
        return Ok(uninitialized_tick_array(expected_start_tick_index));
    };

    let tick_array = TickArray::from_bytes(data).context("failed to decode Orca TickArray")?;
    let facade: TickArrayFacade = tick_array.into();
    if facade.start_tick_index != expected_start_tick_index {
        bail!(
            "Orca TickArray start index mismatch: expected {}, got {}",
            expected_start_tick_index,
            facade.start_tick_index
        );
    }
    Ok(facade)
}

/// Adaptive Fee 池需要额外读取 Oracle；普通费率池不需要。
pub fn needs_oracle(whirlpool: &Whirlpool) -> bool {
    whirlpool.tick_spacing != u16::from_le_bytes(whirlpool.fee_tier_index_seed)
}

/// 调用 Orca 官方 `orca_whirlpools_core` 计算 exact-input 报价。
/// 当前调用者会先确认两个 Mint 都属于经典 SPL Token，因此这里暂不传 Token-2022 transfer fee。
pub fn quote_exact_in(
    whirlpool: &Whirlpool,
    tick_arrays: [TickArrayFacade; 5],
    oracle: Option<OracleFacade>,
    input_mint: &str,
    amount_in: u64,
    timestamp: u64,
) -> Result<ExactInSwapQuote> {
    if amount_in == 0 {
        bail!("Orca quote input amount must be positive");
    }
    let input_mint = Pubkey::from_str(input_mint).context("invalid Orca input mint")?;
    let specified_token_a = if input_mint == whirlpool.token_mint_a {
        true
    } else if input_mint == whirlpool.token_mint_b {
        false
    } else {
        bail!("input mint is not part of this Orca Whirlpool");
    };
n
    swap_quote_by_input_token(
        amount_in,
        specified_token_a,
        0,
        whirlpool.clone().into(),
        oracle,
        tick_arrays.into(),
        timestamp,
        None,
        None,
    )
    .map_err(|error| anyhow::anyhow!("Orca core quote failed: {error:?}"))
}

fn uninitialized_tick_array(start_tick_index: i32) -> TickArrayFacade {
    TickArrayFacade {
        start_tick_index,
        ticks: [TickFacade::default(); TICK_ARRAY_SIZE],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn empty_whirlpool() -> Whirlpool {
        let mut data = vec![0u8; Whirlpool::LEN];
        data[..8].copy_from_slice(&WHIRLPOOL_DISCRIMINATOR);
        decode_whirlpool(&data).unwrap()
    }

    #[test]
    fn decodes_official_whirlpool_layout_and_rejects_bad_header() {
        let whirlpool = empty_whirlpool();
        assert_eq!(whirlpool.discriminator, WHIRLPOOL_DISCRIMINATOR);

        let mut wrong_discriminator = vec![0u8; Whirlpool::LEN];
        wrong_discriminator[..8].copy_from_slice(&[1u8; 8]);
        assert!(decode_whirlpool(&wrong_discriminator).is_err());
        assert!(decode_whirlpool(&[0u8; Whirlpool::LEN - 1]).is_err());
    }

    #[test]
    fn decodes_official_oracle_layout_and_rejects_bad_header() {
        let mut data = vec![0u8; Oracle::LEN];
        data[..8].copy_from_slice(&ORACLE_DISCRIMINATOR);
        let oracle = decode_oracle(&data).unwrap();
        assert_eq!(oracle.discriminator, ORACLE_DISCRIMINATOR);

        data[0] ^= 1;
        assert!(decode_oracle(&data).is_err());
        assert!(decode_oracle(&[0u8; Oracle::LEN - 1]).is_err());
    }

    #[test]
    fn derives_five_tick_array_indexes_around_current_array() {
        let indexes = tick_array_start_indexes(123, 64);
        let offset = 64 * TICK_ARRAY_SIZE as i32;
        assert_eq!(indexes[1] - indexes[0], offset);
        assert_eq!(indexes[2] - indexes[1], offset);
        assert_eq!(indexes[0] - indexes[3], offset);
        assert_eq!(indexes[3] - indexes[4], offset);
    }

    #[test]
    fn missing_tick_array_becomes_uninitialized_array_at_expected_index() {
        let facade = decode_tick_array_or_default(None, -5_632).unwrap();
        assert_eq!(facade.start_tick_index, -5_632);
        assert!(facade.ticks.iter().all(|tick| !tick.initialized));
        assert!(decode_tick_array_or_default(Some(&[1, 2, 3]), 0).is_err());
    }

    #[test]
    fn detects_adaptive_fee_from_fee_tier_seed() {
        let mut whirlpool = empty_whirlpool();
        whirlpool.tick_spacing = 64;
        whirlpool.fee_tier_index_seed = 64u16.to_le_bytes();
        assert!(!needs_oracle(&whirlpool));

        whirlpool.fee_tier_index_seed = 1u16.to_le_bytes();
        assert!(needs_oracle(&whirlpool));
    }

    #[test]
    fn quote_wrapper_rejects_zero_amount_and_unknown_mint_before_core_math() {
        let whirlpool = empty_whirlpool();
        let tick_arrays = tick_array_start_indexes(0, 1).map(uninitialized_tick_array);
        assert!(quote_exact_in(
            &whirlpool,
            tick_arrays,
            None,
            "So11111111111111111111111111111111111111112",
            0,
            1,
        )
        .is_err());

        let tick_arrays = tick_array_start_indexes(0, 1).map(uninitialized_tick_array);
        assert!(quote_exact_in(
            &whirlpool,
            tick_arrays,
            None,
            "So11111111111111111111111111111111111111112",
            1,
            1,
        )
        .is_err());
    }
}
