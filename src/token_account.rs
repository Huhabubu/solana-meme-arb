use anyhow::{bail, Context, Result};

pub const SPL_TOKEN_PROGRAM_ID: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
pub const SPL_TOKEN_ACCOUNT_LEN: usize = 165;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplTokenAccount {
    pub mint: String,
    pub amount: u64,
}

/// Raydium AMM v4 的 vault 使用经典 SPL Token Account；mint 位于 0..32，amount 位于 64..72。
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
        assert!(decode_spl_token_account(&vec![0u8; SPL_TOKEN_ACCOUNT_LEN - 1]).is_err());
        assert!(decode_spl_token_account(&vec![0u8; SPL_TOKEN_ACCOUNT_LEN + 1]).is_err());
    }
}
