use anyhow::{bail, Context, Result};

pub const SPL_TOKEN_PROGRAM_ID: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
pub const SPL_TOKEN_ACCOUNT_LEN: usize = 165;
pub const SPL_TOKEN_MINT_LEN: usize = 82;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplTokenAccount {
    pub mint: String,
    pub amount: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplTokenMint {
    pub supply: u64,
    pub decimals: u8,
    pub is_initialized: bool,
}

/// 经典 SPL Token Account：mint 位于 0..32，amount 位于 64..72。
pub fn decode_spl_token_account(data: &[u8]) -> Result<SplTokenAccount> {
    if data.len() != SPL_TOKEN_ACCOUNT_LEN {
        bail!(
            "SPL token account length mismatch: expected {}, got {}",
            SPL_TOKEN_ACCOUNT_LEN,
            data.len()
        );
    }

    let mint = bs58::encode(
        data.get(0..32)
            .context("SPL token account missing mint bytes")?,
    )
    .into_string();
    let amount_bytes: [u8; 8] = data
        .get(64..72)
        .context("SPL token account missing amount bytes")?
        .try_into()
        .expect("slice length checked above");

    Ok(SplTokenAccount {
        mint,
        amount: u64::from_le_bytes(amount_bytes),
    })
}

/// 经典 SPL Token Mint 的官方布局为 82 字节：supply 位于 36..44，decimals 位于 44，初始化标志位于 45。
pub fn decode_spl_token_mint(data: &[u8]) -> Result<SplTokenMint> {
    if data.len() != SPL_TOKEN_MINT_LEN {
        bail!(
            "SPL token mint length mismatch: expected {}, got {}",
            SPL_TOKEN_MINT_LEN,
            data.len()
        );
    }

    let supply_bytes: [u8; 8] = data
        .get(36..44)
        .context("SPL token mint missing supply bytes")?
        .try_into()
        .expect("slice length checked above");
    let decimals = *data.get(44).context("SPL token mint missing decimals")?;
    let is_initialized = match data.get(45).copied() {
        Some(0) => false,
        Some(1) => true,
        Some(_) => bail!("SPL token mint has invalid initialized flag"),
        None => bail!("SPL token mint missing initialized flag"),
    };

    Ok(SplTokenMint {
        supply: u64::from_le_bytes(supply_bytes),
        decimals,
        is_initialized,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_mint_and_amount_from_classic_spl_token_account() {
        let mut data = vec![0u8; SPL_TOKEN_ACCOUNT_LEN];
        let mint_bytes = [7u8; 32];
        data[0..32].copy_from_slice(&mint_bytes);
        data[64..72].copy_from_slice(&123_456_789u64.to_le_bytes());

        let account = decode_spl_token_account(&data).unwrap();
        assert_eq!(account.mint, bs58::encode(mint_bytes).into_string());
        assert_eq!(account.amount, 123_456_789);
    }

    #[test]
    fn rejects_non_classic_token_account_length() {
        assert!(decode_spl_token_account(&[0u8; SPL_TOKEN_ACCOUNT_LEN - 1]).is_err());
        assert!(decode_spl_token_account(&[0u8; SPL_TOKEN_ACCOUNT_LEN + 1]).is_err());
    }

    #[test]
    fn decodes_supply_decimals_and_initialized_flag_from_classic_mint() {
        let mut data = [0u8; SPL_TOKEN_MINT_LEN];
        data[36..44].copy_from_slice(&987_654_321u64.to_le_bytes());
        data[44] = 6;
        data[45] = 1;

        let mint = decode_spl_token_mint(&data).unwrap();
        assert_eq!(mint.supply, 987_654_321);
        assert_eq!(mint.decimals, 6);
        assert!(mint.is_initialized);
    }

    #[test]
    fn mint_decoder_rejects_wrong_length_and_invalid_initialized_flag() {
        assert!(decode_spl_token_mint(&[0u8; SPL_TOKEN_MINT_LEN - 1]).is_err());

        let mut data = [0u8; SPL_TOKEN_MINT_LEN];
        data[45] = 2;
        assert!(decode_spl_token_mint(&data).is_err());
    }
}
