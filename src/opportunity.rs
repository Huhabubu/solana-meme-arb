use anyhow::{bail, Result};

use crate::model::Dex;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SwapQuote {
    pub dex: Dex,
    pub pool_address: String,
    pub input_mint: String,
    pub output_mint: String,
    pub amount_in: u64,
    pub amount_out: u64,
    pub snapshot_slot: u64,
}

impl SwapQuote {
    pub fn new(
        dex: Dex,
        pool_address: impl Into<String>,
        input_mint: impl Into<String>,
        output_mint: impl Into<String>,
        amount_in: u64,
        amount_out: u64,
        snapshot_slot: u64,
    ) -> Result<Self> {
        let pool_address = pool_address.into();
        let input_mint = input_mint.into();
        let output_mint = output_mint.into();

        if pool_address.is_empty() {
            bail!("swap quote pool address must not be empty");
        }
        if input_mint.is_empty() || output_mint.is_empty() {
            bail!("swap quote mint must not be empty");
        }
        if input_mint == output_mint {
            bail!("swap quote input and output mint must differ");
        }
        if amount_in == 0 || amount_out == 0 {
            bail!("swap quote amounts must be positive");
        }

        Ok(Self {
            dex,
            pool_address,
            input_mint,
            output_mint,
            amount_in,
            amount_out,
            snapshot_slot,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoundTripOpportunity {
    pub first_leg: SwapQuote,
    pub second_leg: SwapQuote,
    pub base_mint: String,
    pub intermediate_mint: String,
    pub input_amount: u64,
    pub intermediate_amount: u64,
    pub final_amount: u64,
    pub gross_profit_raw: i128,
    pub gross_return_bps: i128,
    pub oldest_slot: u64,
    pub newest_slot: u64,
}

/// 为 N 个不同池生成全部有向两池路径索引；每个池都可以作为第一腿或第二腿，
/// 但同一池不会和自己组成套利闭环。
pub fn directed_route_indices(pool_count: usize) -> Vec<(usize, usize)> {
    let mut routes = Vec::with_capacity(pool_count.saturating_mul(pool_count.saturating_sub(1)));
    for first in 0..pool_count {
        for second in 0..pool_count {
            if first != second {
                routes.push((first, second));
            }
        }
    }
    routes
}

/// 评估两腿 exact-input 闭环。DEX swap fee 已经包含在各腿 Quote 输出中；
/// 这里的 gross profit 只是不再扣 Priority Fee / Jito Tip 等执行成本。
pub fn evaluate_round_trip(
    first_leg: &SwapQuote,
    second_leg: &SwapQuote,
) -> Result<RoundTripOpportunity> {
    if first_leg.pool_address == second_leg.pool_address {
        bail!("round trip must use two different pools");
    }
    if first_leg.output_mint != second_leg.input_mint {
        bail!("second leg input mint does not match first leg output mint");
    }
    if first_leg.input_mint != second_leg.output_mint {
        bail!("second leg does not return to the original base mint");
    }
    if first_leg.amount_out != second_leg.amount_in {
        bail!("second leg input amount must equal first leg output amount");
    }

    let gross_profit_raw = i128::from(second_leg.amount_out) - i128::from(first_leg.amount_in);
    let gross_return_bps = gross_profit_raw
        .checked_mul(10_000)
        .expect("u64-sized quote difference multiplied by 10,000 fits i128")
        / i128::from(first_leg.amount_in);

    Ok(RoundTripOpportunity {
        first_leg: first_leg.clone(),
        second_leg: second_leg.clone(),
        base_mint: first_leg.input_mint.clone(),
        intermediate_mint: first_leg.output_mint.clone(),
        input_amount: first_leg.amount_in,
        intermediate_amount: first_leg.amount_out,
        final_amount: second_leg.amount_out,
        gross_profit_raw,
        gross_return_bps,
        oldest_slot: first_leg.snapshot_slot.min(second_leg.snapshot_slot),
        newest_slot: first_leg.snapshot_slot.max(second_leg.snapshot_slot),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quote(
        dex: Dex,
        pool: &str,
        input_mint: &str,
        output_mint: &str,
        amount_in: u64,
        amount_out: u64,
        slot: u64,
    ) -> SwapQuote {
        SwapQuote::new(
            dex,
            pool,
            input_mint,
            output_mint,
            amount_in,
            amount_out,
            slot,
        )
        .unwrap()
    }

    #[test]
    fn swap_quote_constructor_rejects_invalid_identity_and_amounts() {
        assert!(SwapQuote::new(Dex::Raydium, "", "A", "B", 1, 1, 1).is_err());
        assert!(SwapQuote::new(Dex::Raydium, "pool", "", "B", 1, 1, 1).is_err());
        assert!(SwapQuote::new(Dex::Raydium, "pool", "A", "A", 1, 1, 1).is_err());
        assert!(SwapQuote::new(Dex::Raydium, "pool", "A", "B", 0, 1, 1).is_err());
        assert!(SwapQuote::new(Dex::Raydium, "pool", "A", "B", 1, 0, 1).is_err());
    }

    #[test]
    fn directed_routes_cover_all_ordered_distinct_pairs() {
        assert!(directed_route_indices(0).is_empty());
        assert!(directed_route_indices(1).is_empty());
        assert_eq!(
            directed_route_indices(3),
            vec![(0, 1), (0, 2), (1, 0), (1, 2), (2, 0), (2, 1)]
        );
    }

    #[test]
    fn evaluates_profitable_round_trip_and_preserves_slot_range() {
        let first = quote(Dex::Orca, "orca", "SOL", "TOKEN", 1_000_000, 2_000_000, 100);
        let second = quote(
            Dex::Raydium,
            "raydium",
            "TOKEN",
            "SOL",
            2_000_000,
            1_010_000,
            103,
        );

        let opportunity = evaluate_round_trip(&first, &second).unwrap();
        assert_eq!(opportunity.base_mint, "SOL");
        assert_eq!(opportunity.intermediate_mint, "TOKEN");
        assert_eq!(opportunity.input_amount, 1_000_000);
        assert_eq!(opportunity.intermediate_amount, 2_000_000);
        assert_eq!(opportunity.final_amount, 1_010_000);
        assert_eq!(opportunity.gross_profit_raw, 10_000);
        assert_eq!(opportunity.gross_return_bps, 100);
        assert_eq!(opportunity.oldest_slot, 100);
        assert_eq!(opportunity.newest_slot, 103);
    }

    #[test]
    fn loss_is_signed_instead_of_underflowing() {
        let first = quote(Dex::Raydium, "a", "SOL", "TOKEN", 1_000, 2_000, 10);
        let second = quote(Dex::Orca, "b", "TOKEN", "SOL", 2_000, 990, 9);

        let opportunity = evaluate_round_trip(&first, &second).unwrap();
        assert_eq!(opportunity.gross_profit_raw, -10);
        assert_eq!(opportunity.gross_return_bps, -100);
        assert_eq!(opportunity.oldest_slot, 9);
        assert_eq!(opportunity.newest_slot, 10);
    }

    #[test]
    fn rejects_same_pool_broken_mints_and_amount_discontinuity() {
        let first = quote(Dex::Raydium, "same", "SOL", "TOKEN", 100, 200, 1);
        let same_pool = quote(Dex::Raydium, "same", "TOKEN", "SOL", 200, 101, 1);
        assert!(evaluate_round_trip(&first, &same_pool).is_err());

        let wrong_input_mint = quote(Dex::Orca, "b", "OTHER", "SOL", 200, 101, 1);
        assert!(evaluate_round_trip(&first, &wrong_input_mint).is_err());

        let wrong_output_mint = quote(Dex::Orca, "b", "TOKEN", "OTHER", 200, 101, 1);
        assert!(evaluate_round_trip(&first, &wrong_output_mint).is_err());

        let wrong_amount = quote(Dex::Orca, "b", "TOKEN", "SOL", 199, 101, 1);
        assert!(evaluate_round_trip(&first, &wrong_amount).is_err());
    }
}
