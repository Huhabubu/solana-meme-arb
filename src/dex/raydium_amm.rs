use anyhow::{bail, Context, Result};

pub const RAYDIUM_AMM_V4_PROGRAM_ID: &str = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
pub const RAYDIUM_AMM_V4_ACCOUNT_LEN: usize = 752;

const STATUS_OFFSET: usize = 0;
const COIN_DECIMALS_OFFSET: usize = 32;
const PC_DECIMALS_OFFSET: usize = 40;
const SWAP_FEE_NUMERATOR_OFFSET: usize = 176;
const SWAP_FEE_DENOMINATOR_OFFSET: usize = 184;
const NEED_TAKE_PNL_COIN_OFFSET: usize = 192;
const NEED_TAKE_PNL_PC_OFFSET: usize = 200;
const POOL_OPEN_TIME_OFFSET: usize = 224;
const COIN_VAULT_OFFSET: usize = 336;
const PC_VAULT_OFFSET: usize = 368;
const COIN_MINT_OFFSET: usize = 400;
const PC_MINT_OFFSET: usize = 432;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RaydiumAmmV4State {
    pub status: u64,
    pub coin_decimals: u64,
    pub pc_decimals: u64,
    pub swap_fee_numerator: u64,
    pub swap_fee_denominator: u64,
    pub need_take_pnl_coin: u64,
    pub need_take_pnl_pc: u64,
    pub pool_open_time: u64,
    pub coin_vault: String,
    pub pc_vault: String,
    pub coin_mint: String,
    pub pc_mint: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RaydiumQuote {
    pub input_mint: String,
    pub output_mint: String,
    pub amount_in: u64,
    pub fee_amount: u64,
    pub amount_out: u64,
}

/// 按 Raydium 当前 AMM v4 `AmmInfo` 的 752 字节 packed 布局读取报价所需字段。
pub fn decode_amm_v4(data: &[u8]) -> Result<RaydiumAmmV4State> {
    if data.len() != RAYDIUM_AMM_V4_ACCOUNT_LEN {
        bail!(
            "Raydium AMM v4 account length mismatch: expected {}, got {}",
            RAYDIUM_AMM_V4_ACCOUNT_LEN,
            data.len()
        );
    }

    let state = RaydiumAmmV4State {
        status: read_u64(data, STATUS_OFFSET)?,
        coin_decimals: read_u64(data, COIN_DECIMALS_OFFSET)?,
        pc_decimals: read_u64(data, PC_DECIMALS_OFFSET)?,
        swap_fee_numerator: read_u64(data, SWAP_FEE_NUMERATOR_OFFSET)?,
        swap_fee_denominator: read_u64(data, SWAP_FEE_DENOMINATOR_OFFSET)?,
        need_take_pnl_coin: read_u64(data, NEED_TAKE_PNL_COIN_OFFSET)?,
        need_take_pnl_pc: read_u64(data, NEED_TAKE_PNL_PC_OFFSET)?,
        pool_open_time: read_u64(data, POOL_OPEN_TIME_OFFSET)?,
        coin_vault: read_pubkey(data, COIN_VAULT_OFFSET)?,
        pc_vault: read_pubkey(data, PC_VAULT_OFFSET)?,
        coin_mint: read_pubkey(data, COIN_MINT_OFFSET)?,
        pc_mint: read_pubkey(data, PC_MINT_OFFSET)?,
    };

    if state.swap_fee_denominator == 0 {
        bail!("Raydium AMM v4 swap fee denominator is zero");
    }
    if state.swap_fee_numerator >= state.swap_fee_denominator {
        bail!("Raydium AMM v4 swap fee is not a valid fraction");
    }

    Ok(state)
}

/// 与当前 Raydium `SwapBaseInV2` 程序路径保持一致：vault 余额先扣待提取 PnL。
pub fn effective_reserves(
    state: &RaydiumAmmV4State,
    coin_vault_amount: u64,
    pc_vault_amount: u64,
) -> Result<(u64, u64)> {
    let coin = coin_vault_amount
        .checked_sub(state.need_take_pnl_coin)
        .context("coin vault amount is below need_take_pnl_coin")?;
    let pc = pc_vault_amount
        .checked_sub(state.need_take_pnl_pc)
        .context("pc vault amount is below need_take_pnl_pc")?;
    if coin == 0 || pc == 0 {
        bail!("Raydium AMM v4 effective reserve is zero");
    }
    Ok((coin, pc))
}

/// 计算 exact-input 报价；输出采用链上程序同样的整数向下取整规则。
pub fn quote_base_in(
    state: &RaydiumAmmV4State,
    coin_vault_amount: u64,
    pc_vault_amount: u64,
    input_mint: &str,
    amount_in: u64,
    unix_timestamp: u64,
) -> Result<RaydiumQuote> {
    if amount_in == 0 {
        bail!("Raydium quote input amount must be positive");
    }
    if !swap_is_open(state.status, state.pool_open_time, unix_timestamp) {
        bail!("Raydium AMM v4 pool status does not currently allow swaps");
    }

    let (coin_reserve, pc_reserve) = effective_reserves(state, coin_vault_amount, pc_vault_amount)?;
    let fee_amount = ceil_fraction(
        amount_in,
        state.swap_fee_numerator,
        state.swap_fee_denominator,
    )?;
    let amount_after_fee = amount_in
        .checked_sub(fee_amount)
        .context("Raydium swap fee consumes the full input")?;
    if amount_after_fee == 0 {
        bail!("Raydium swap fee leaves zero tradable input");
    }

    let (input_reserve, output_reserve, output_mint) = if input_mint == state.coin_mint {
        (coin_reserve, pc_reserve, state.pc_mint.as_str())
    } else if input_mint == state.pc_mint {
        (pc_reserve, coin_reserve, state.coin_mint.as_str())
    } else {
        bail!("input mint is not part of this Raydium AMM v4 pool");
    };

    // Raydium on-chain formula: output = reserve_out * net_input / (reserve_in + net_input).
    let numerator = (output_reserve as u128)
        .checked_mul(amount_after_fee as u128)
        .context("Raydium quote numerator overflow")?;
    let denominator = (input_reserve as u128)
        .checked_add(amount_after_fee as u128)
        .context("Raydium quote denominator overflow")?;
    let amount_out = numerator / denominator;
    let amount_out = u64::try_from(amount_out).context("Raydium quote output exceeds u64")?;
    if amount_out == 0 || amount_out >= output_reserve {
        bail!("Raydium quote produced an invalid output amount");
    }

    Ok(RaydiumQuote {
        input_mint: input_mint.to_owned(),
        output_mint: output_mint.to_owned(),
        amount_in,
        fee_amount,
        amount_out,
    })
}

fn swap_is_open(status: u64, pool_open_time: u64, unix_timestamp: u64) -> bool {
    match status {
        // Initialized and SwapOnly.
        1 | 6 => true,
        // WaitingTrade becomes swappable once pool_open_time is reached.
        7 => unix_timestamp >= pool_open_time,
        _ => false,
    }
}

fn ceil_fraction(value: u64, numerator: u64, denominator: u64) -> Result<u64> {
    if denominator == 0 {
        bail!("fraction denominator is zero");
    }
    let product = (value as u128)
        .checked_mul(numerator as u128)
        .context("fraction multiplication overflow")?;
    let result = product
        .checked_add(denominator as u128 - 1)
        .context("fraction ceiling overflow")?
        / denominator as u128;
    u64::try_from(result).context("fraction result exceeds u64")
}

fn read_u64(data: &[u8], offset: usize) -> Result<u64> {
    let bytes: [u8; 8] = data
        .get(offset..offset + 8)
        .context("u64 field exceeds account data")?
        .try_into()
        .expect("slice length checked above");
    Ok(u64::from_le_bytes(bytes))
}

fn read_pubkey(data: &[u8], offset: usize) -> Result<String> {
    let bytes = data
        .get(offset..offset + 32)
        .context("pubkey field exceeds account data")?;
    Ok(bs58::encode(bytes).into_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn put_u64(data: &mut [u8], offset: usize, value: u64) {
        data[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
    }

    fn put_pubkey(data: &mut [u8], offset: usize, byte: u8) -> String {
        let bytes = [byte; 32];
        data[offset..offset + 32].copy_from_slice(&bytes);
        bs58::encode(bytes).into_string()
    }

    fn sample_state() -> RaydiumAmmV4State {
        RaydiumAmmV4State {
            status: 6,
            coin_decimals: 9,
            pc_decimals: 5,
            swap_fee_numerator: 25,
            swap_fee_denominator: 10_000,
            need_take_pnl_coin: 100,
            need_take_pnl_pc: 200,
            pool_open_time: 1_000,
            coin_vault: "coin-vault".into(),
            pc_vault: "pc-vault".into(),
            coin_mint: "coin".into(),
            pc_mint: "pc".into(),
        }
    }

    #[test]
    fn decodes_current_amm_v4_offsets() {
        let mut data = vec![0u8; RAYDIUM_AMM_V4_ACCOUNT_LEN];
        put_u64(&mut data, STATUS_OFFSET, 6);
        put_u64(&mut data, COIN_DECIMALS_OFFSET, 9);
        put_u64(&mut data, PC_DECIMALS_OFFSET, 5);
        put_u64(&mut data, SWAP_FEE_NUMERATOR_OFFSET, 25);
        put_u64(&mut data, SWAP_FEE_DENOMINATOR_OFFSET, 10_000);
        put_u64(&mut data, NEED_TAKE_PNL_COIN_OFFSET, 111);
        put_u64(&mut data, NEED_TAKE_PNL_PC_OFFSET, 222);
        put_u64(&mut data, POOL_OPEN_TIME_OFFSET, 333);
        let coin_vault = put_pubkey(&mut data, COIN_VAULT_OFFSET, 1);
        let pc_vault = put_pubkey(&mut data, PC_VAULT_OFFSET, 2);
        let coin_mint = put_pubkey(&mut data, COIN_MINT_OFFSET, 3);
        let pc_mint = put_pubkey(&mut data, PC_MINT_OFFSET, 4);

        let state = decode_amm_v4(&data).unwrap();
        assert_eq!(state.status, 6);
        assert_eq!(state.coin_decimals, 9);
        assert_eq!(state.pc_decimals, 5);
        assert_eq!(state.swap_fee_numerator, 25);
        assert_eq!(state.swap_fee_denominator, 10_000);
        assert_eq!(state.need_take_pnl_coin, 111);
        assert_eq!(state.need_take_pnl_pc, 222);
        assert_eq!(state.pool_open_time, 333);
        assert_eq!(state.coin_vault, coin_vault);
        assert_eq!(state.pc_vault, pc_vault);
        assert_eq!(state.coin_mint, coin_mint);
        assert_eq!(state.pc_mint, pc_mint);
    }

    #[test]
    fn decoder_rejects_wrong_length_and_invalid_fee() {
        assert!(decode_amm_v4(&vec![0; 751]).is_err());

        let mut data = vec![0u8; RAYDIUM_AMM_V4_ACCOUNT_LEN];
        put_u64(&mut data, SWAP_FEE_NUMERATOR_OFFSET, 10_000);
        put_u64(&mut data, SWAP_FEE_DENOMINATOR_OFFSET, 10_000);
        assert!(decode_amm_v4(&data).is_err());
    }

    #[test]
    fn effective_reserves_subtract_pending_pnl() {
        let state = sample_state();
        assert_eq!(
            effective_reserves(&state, 10_000, 20_000).unwrap(),
            (9_900, 19_800)
        );
        assert!(effective_reserves(&state, 99, 20_000).is_err());
    }

    #[test]
    fn quote_coin_to_pc_matches_program_formula_and_ceiling_fee() {
        let state = sample_state();
        let quote = quote_base_in(&state, 1_000_100, 2_000_200, "coin", 101, 2_000).unwrap();
        // ceil(101 * 25 / 10_000) = 1; effective reserves are exactly 1,000,000 / 2,000,000.
        assert_eq!(quote.fee_amount, 1);
        assert_eq!(quote.amount_out, 2_000_000u64 * 100 / 1_000_100);
        assert_eq!(quote.output_mint, "pc");
    }

    #[test]
    fn quote_pc_to_coin_uses_reverse_reserves() {
        let state = sample_state();
        let quote = quote_base_in(&state, 1_000_100, 2_000_200, "pc", 10_000, 2_000).unwrap();
        let fee = 25u64;
        let net = 10_000 - fee;
        assert_eq!(quote.fee_amount, fee);
        assert_eq!(quote.amount_out, 1_000_000u64 * net / (2_000_000 + net));
        assert_eq!(quote.output_mint, "coin");
    }

    #[test]
    fn quote_rejects_closed_pool_unknown_mint_and_zero_input() {
        let mut state = sample_state();
        state.status = 4;
        assert!(quote_base_in(&state, 1_000_100, 2_000_200, "coin", 100, 2_000).is_err());

        state.status = 6;
        assert!(quote_base_in(&state, 1_000_100, 2_000_200, "other", 100, 2_000).is_err());
        assert!(quote_base_in(&state, 1_000_100, 2_000_200, "coin", 0, 2_000).is_err());
    }

    #[test]
    fn waiting_trade_respects_pool_open_time() {
        let mut state = sample_state();
        state.status = 7;
        assert!(quote_base_in(&state, 1_000_100, 2_000_200, "coin", 100, 999).is_err());
        assert!(quote_base_in(&state, 1_000_100, 2_000_200, "coin", 100, 1_000).is_ok());
    }

    #[test]
    fn ceil_fraction_handles_exact_and_non_exact_division() {
        assert_eq!(ceil_fraction(10_000, 25, 10_000).unwrap(), 25);
        assert_eq!(ceil_fraction(101, 25, 10_000).unwrap(), 1);
        assert!(ceil_fraction(1, 1, 0).is_err());
    }
}
