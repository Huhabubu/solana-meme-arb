use std::{collections::HashMap, mem::size_of, str::FromStr};

use anchor_client::solana_sdk::{
    account::Account,
    clock::Clock,
    pubkey::Pubkey,
    sysvar,
};
use anchor_lang::Discriminator;
use anyhow::{bail, Context, Result};
use commons::dlmm::accounts::{BinArray, BinArrayBitmapExtension, LbPair};
use commons::{
    derive_bin_array_bitmap_extension, get_bin_array_pubkeys_for_swap,
    pod_read_unaligned_skip_disc, quote_exact_in as meteora_quote_exact_in, SwapExactInQuote,
};

/// Meteora 官方 Rust SDK 的账户类型是 zero-copy POD；这里额外校验 Anchor discriminator，
/// 防止“长度正确但账户类型错误”的字节被当成 LbPair。
pub fn decode_lb_pair(data: &[u8]) -> Result<LbPair> {
    validate_anchor_account::<LbPair>(data, size_of::<LbPair>(), "LbPair")?;
    pod_read_unaligned_skip_disc(data).context("failed to decode Meteora LbPair")
}

pub fn decode_bitmap_extension(data: &[u8]) -> Result<BinArrayBitmapExtension> {
    validate_anchor_account::<BinArrayBitmapExtension>(
        data,
        size_of::<BinArrayBitmapExtension>(),
        "BinArrayBitmapExtension",
    )?;
    pod_read_unaligned_skip_disc(data).context("failed to decode Meteora bitmap extension")
}

pub fn decode_bin_array(data: &[u8]) -> Result<BinArray> {
    validate_anchor_account::<BinArray>(data, size_of::<BinArray>(), "BinArray")?;
    pod_read_unaligned_skip_disc(data).context("failed to decode Meteora BinArray")
}

/// `swap_for_y=true` 表示卖 token X、买 token Y；false 表示卖 Y、买 X。
pub fn swap_for_y_for_input(lb_pair: &LbPair, input_mint: &str) -> Result<bool> {
    let input = Pubkey::from_str(input_mint).context("invalid Meteora input mint")?;
    if input == lb_pair.token_x_mint {
        Ok(true)
    } else if input == lb_pair.token_y_mint {
        Ok(false)
    } else {
        bail!("input mint is not part of this Meteora LbPair");
    }
}

pub fn bitmap_extension_address(lb_pair: &str) -> Result<String> {
    let lb_pair = Pubkey::from_str(lb_pair).context("invalid Meteora LbPair address")?;
    Ok(derive_bin_array_bitmap_extension(lb_pair).0.to_string())
}

pub fn clock_sysvar_address() -> String {
    sysvar::clock::id().to_string()
}

/// 直接调用 Meteora 官方 `get_bin_array_pubkeys_for_swap`，由 LbPair 的内部 bitmap
/// 和可选 extension 决定当前方向真正需要读取哪些 BinArray。
pub fn bin_array_addresses_for_swap(
    lb_pair_address: &str,
    lb_pair: &LbPair,
    bitmap_extension: Option<&BinArrayBitmapExtension>,
    swap_for_y: bool,
    take_count: u8,
) -> Result<Vec<String>> {
    if take_count == 0 {
        return Ok(Vec::new());
    }
    let lb_pair_pubkey =
        Pubkey::from_str(lb_pair_address).context("invalid Meteora LbPair address")?;
    get_bin_array_pubkeys_for_swap(
        lb_pair_pubkey,
        lb_pair,
        bitmap_extension,
        swap_for_y,
        take_count,
    )
    .map(|keys| keys.into_iter().map(|key| key.to_string()).collect())
}

pub fn build_bin_array_map(entries: Vec<(String, BinArray)>) -> Result<HashMap<Pubkey, BinArray>> {
    let mut map = HashMap::with_capacity(entries.len());
    for (address, bin_array) in entries {
        let pubkey = Pubkey::from_str(&address).context("invalid Meteora BinArray address")?;
        if map.insert(pubkey, bin_array).is_some() {
            bail!("duplicate Meteora BinArray address: {address}");
        }
    }
    Ok(map)
}

pub fn decode_clock(data: &[u8]) -> Result<Clock> {
    bincode::deserialize(data).context("failed to decode Solana Clock sysvar")
}

/// 将通用 RPC 读取到的 owner + data 转成 Meteora 官方 Quote 所要求的 Solana v2 Account。
/// Quote 只依赖 mint owner / data；其余账户元数据不参与本地数学，因此保持为零值。
pub fn quote_mint_account(owner: &str, data: &[u8]) -> Result<Account> {
    Ok(Account {
        lamports: 0,
        data: data.to_vec(),
        owner: Pubkey::from_str(owner).context("invalid token mint owner")?,
        executable: false,
        rent_epoch: 0,
    })
}

/// 最终报价完全委托给 Meteora 官方 Rust `commons::quote_exact_in`。
#[allow(clippy::too_many_arguments)]
pub fn quote_exact_in(
    lb_pair_address: &str,
    lb_pair: &LbPair,
    amount_in: u64,
    swap_for_y: bool,
    bin_arrays: HashMap<Pubkey, BinArray>,
    bitmap_extension: Option<&BinArrayBitmapExtension>,
    clock: &Clock,
    mint_x_account: &Account,
    mint_y_account: &Account,
) -> Result<SwapExactInQuote> {
    if amount_in == 0 {
        bail!("Meteora quote input amount must be positive");
    }
    let lb_pair_pubkey =
        Pubkey::from_str(lb_pair_address).context("invalid Meteora LbPair address")?;
    meteora_quote_exact_in(
        lb_pair_pubkey,
        lb_pair,
        amount_in,
        swap_for_y,
        bin_arrays,
        bitmap_extension,
        clock,
        mint_x_account,
        mint_y_account,
    )
    .context("Meteora official exact-input quote failed")
}

fn validate_anchor_account<T: Discriminator>(
    data: &[u8],
    body_size: usize,
    account_name: &str,
) -> Result<()> {
    let expected_len = 8usize
        .checked_add(body_size)
        .context("Meteora account size overflow")?;
    if data.len() < expected_len {
        bail!(
            "Meteora {account_name} account too short: expected at least {expected_len}, got {}",
            data.len()
        );
    }
    if data.get(..T::DISCRIMINATOR.len()) != Some(T::DISCRIMINATOR) {
        bail!("Meteora {account_name} discriminator mismatch");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn zeroed_anchor_account<T: Discriminator>(body_size: usize) -> Vec<u8> {
        let mut data = vec![0u8; 8 + body_size];
        data[..T::DISCRIMINATOR.len()].copy_from_slice(T::DISCRIMINATOR);
        data
    }

    #[test]
    fn decodes_zeroed_official_lb_pair_layout_and_rejects_wrong_header() {
        let data = zeroed_anchor_account::<LbPair>(size_of::<LbPair>());
        let pair = decode_lb_pair(&data).unwrap();
        assert_eq!(pair.active_id, 0);

        let mut wrong = data.clone();
        wrong[0] ^= 1;
        assert!(decode_lb_pair(&wrong).is_err());
        assert!(decode_lb_pair(&data[..data.len() - 1]).is_err());
    }

    #[test]
    fn decodes_zeroed_bitmap_and_bin_array_layouts() {
        let bitmap =
            zeroed_anchor_account::<BinArrayBitmapExtension>(size_of::<BinArrayBitmapExtension>());
        assert!(decode_bitmap_extension(&bitmap).is_ok());

        let bin_array = zeroed_anchor_account::<BinArray>(size_of::<BinArray>());
        assert!(decode_bin_array(&bin_array).is_ok());
    }

    #[test]
    fn direction_matches_token_x_and_y() {
        let data = zeroed_anchor_account::<LbPair>(size_of::<LbPair>());
        let mut pair = decode_lb_pair(&data).unwrap();
        pair.token_x_mint = Pubkey::new_from_array([1u8; 32]);
        pair.token_y_mint = Pubkey::new_from_array([2u8; 32]);

        assert!(swap_for_y_for_input(&pair, &pair.token_x_mint.to_string()).unwrap());
        assert!(!swap_for_y_for_input(&pair, &pair.token_y_mint.to_string()).unwrap());
        assert!(
            swap_for_y_for_input(&pair, &Pubkey::new_from_array([3u8; 32]).to_string()).is_err()
        );
    }

    #[test]
    fn bitmap_and_clock_addresses_are_deterministic() {
        let pair = Pubkey::new_from_array([9u8; 32]).to_string();
        assert_eq!(
            bitmap_extension_address(&pair).unwrap(),
            bitmap_extension_address(&pair).unwrap()
        );
        assert_eq!(clock_sysvar_address(), sysvar::clock::id().to_string());
        assert!(bitmap_extension_address("not-a-pubkey").is_err());
    }

    #[test]
    fn zero_take_count_does_not_require_liquidity() {
        let data = zeroed_anchor_account::<LbPair>(size_of::<LbPair>());
        let pair = decode_lb_pair(&data).unwrap();
        let address = Pubkey::new_from_array([4u8; 32]).to_string();
        assert!(bin_array_addresses_for_swap(&address, &pair, None, true, 0)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn bin_array_map_rejects_duplicate_and_invalid_addresses() {
        let data = zeroed_anchor_account::<BinArray>(size_of::<BinArray>());
        let bin_array = decode_bin_array(&data).unwrap();
        let address = Pubkey::new_from_array([5u8; 32]).to_string();

        assert_eq!(
            build_bin_array_map(vec![(address.clone(), bin_array)]).unwrap().len(),
            1
        );
        assert!(build_bin_array_map(vec![
            (address.clone(), bin_array),
            (address, bin_array)
        ])
        .is_err());
        assert!(build_bin_array_map(vec![("invalid".into(), bin_array)]).is_err());
    }

    #[test]
    fn quote_mint_account_preserves_owner_and_data() {
        let owner = Pubkey::new_from_array([8u8; 32]);
        let account = quote_mint_account(&owner.to_string(), &[1, 2, 3]).unwrap();
        assert_eq!(account.owner, owner);
        assert_eq!(account.data, vec![1, 2, 3]);
        assert!(quote_mint_account("invalid", &[]).is_err());
    }

    #[test]
    fn quote_wrapper_rejects_zero_input_before_official_math() {
        let data = zeroed_anchor_account::<LbPair>(size_of::<LbPair>());
        let pair = decode_lb_pair(&data).unwrap();
        let address = Pubkey::new_from_array([4u8; 32]).to_string();
        let clock = Clock::default();
        let owner = Pubkey::new_from_array([8u8; 32]).to_string();
        let mint = quote_mint_account(&owner, &[]).unwrap();

        assert!(quote_exact_in(
            &address,
            &pair,
            0,
            true,
            HashMap::new(),
            None,
            &clock,
            &mint,
            &mint,
        )
        .is_err());
    }
}
