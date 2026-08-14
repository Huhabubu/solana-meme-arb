use std::collections::HashSet;

use anyhow::{bail, Context, Result};

use crate::model::{Dex, PoolInfo};

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
    pub gross_return_ppm: i128,
    pub oldest_slot: u64,
    pub newest_slot: u64,
}

/// V3 只负责“把已经估计出的执行成本正确计入净利润”。
/// DEX swap fee 已经反映在两腿 Quote 的输出里，因此这里禁止再放 DEX fee，避免重复扣费。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExecutionCost {
    pub base_fee_lamports: u64,
    pub priority_fee_lamports: u64,
    pub jito_tip_lamports: u64,
    pub other_lamports: u64,
}

impl ExecutionCost {
    pub const ZERO: Self = Self {
        base_fee_lamports: 0,
        priority_fee_lamports: 0,
        jito_tip_lamports: 0,
        other_lamports: 0,
    };

    pub fn total_lamports(self) -> Result<u64> {
        self.base_fee_lamports
            .checked_add(self.priority_fee_lamports)
            .and_then(|value| value.checked_add(self.jito_tip_lamports))
            .and_then(|value| value.checked_add(self.other_lamports))
            .context("execution cost overflow")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NetOpportunity {
    pub round_trip: RoundTripOpportunity,
    pub execution_cost: ExecutionCost,
    pub execution_cost_lamports: u64,
    pub net_profit_raw: i128,
    pub net_return_ppm: i128,
}

impl NetOpportunity {
    pub fn is_profitable(&self) -> bool {
        self.net_profit_raw > 0
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DirectedPoolRoute {
    pub token_mint: String,
    pub first_pool: PoolInfo,
    pub second_pool: PoolInfo,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiquidityStage {
    FirstLeg,
    SecondLeg,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OpportunityEventOutcome {
    Evaluated {
        intermediate_amount: u64,
        final_amount: u64,
        gross_profit_raw: i128,
        gross_return_ppm: i128,
        execution_cost_lamports: u64,
        net_profit_raw: i128,
        net_return_ppm: i128,
        oldest_slot: u64,
        newest_slot: u64,
    },
    InsufficientLiquidity {
        stage: LiquidityStage,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpportunityEvent {
    pub token_mint: String,
    pub first_dex: Dex,
    pub first_pool: String,
    pub second_dex: Dex,
    pub second_pool: String,
    pub input_amount: u64,
    pub outcome: OpportunityEventOutcome,
}

impl OpportunityEvent {
    pub fn evaluated(route: &DirectedPoolRoute, net: &NetOpportunity) -> Result<Self> {
        let round_trip = &net.round_trip;
        if route.token_mint != round_trip.intermediate_mint {
            bail!("opportunity event token mint does not match round trip");
        }
        if route.first_pool.address != round_trip.first_leg.pool_address
            || route.first_pool.dex != round_trip.first_leg.dex
            || route.second_pool.address != round_trip.second_leg.pool_address
            || route.second_pool.dex != round_trip.second_leg.dex
        {
            bail!("opportunity event route does not match round trip legs");
        }

        Ok(Self {
            token_mint: route.token_mint.clone(),
            first_dex: route.first_pool.dex,
            first_pool: route.first_pool.address.clone(),
            second_dex: route.second_pool.dex,
            second_pool: route.second_pool.address.clone(),
            input_amount: round_trip.input_amount,
            outcome: OpportunityEventOutcome::Evaluated {
                intermediate_amount: round_trip.intermediate_amount,
                final_amount: round_trip.final_amount,
                gross_profit_raw: round_trip.gross_profit_raw,
                gross_return_ppm: round_trip.gross_return_ppm,
                execution_cost_lamports: net.execution_cost_lamports,
                net_profit_raw: net.net_profit_raw,
                net_return_ppm: net.net_return_ppm,
                oldest_slot: round_trip.oldest_slot,
                newest_slot: round_trip.newest_slot,
            },
        })
    }

    pub fn insufficient_liquidity(
        route: &DirectedPoolRoute,
        input_amount: u64,
        stage: LiquidityStage,
    ) -> Result<Self> {
        if input_amount == 0 {
            bail!("opportunity event input amount must be positive");
        }
        Ok(Self {
            token_mint: route.token_mint.clone(),
            first_dex: route.first_pool.dex,
            first_pool: route.first_pool.address.clone(),
            second_dex: route.second_pool.dex,
            second_pool: route.second_pool.address.clone(),
            input_amount,
            outcome: OpportunityEventOutcome::InsufficientLiquidity { stage },
        })
    }
}

/// 只生成与本次受影响 Pool 有关的有向两池路径，同时严格限制在同一个 Token/WSOL 交易对内。
/// 一个依赖账户若同时影响多个池，最终路径仍按 (first_pool, second_pool) 去重。
pub fn affected_directed_pool_routes(
    pools: &[PoolInfo],
    affected_pool_addresses: &[String],
    base_mint: &str,
) -> Result<Vec<DirectedPoolRoute>> {
    if base_mint.trim().is_empty() {
        bail!("affected-route base mint cannot be empty");
    }
    if affected_pool_addresses.is_empty() {
        return Ok(Vec::new());
    }

    let mut pool_addresses = HashSet::new();
    for pool in pools {
        if !pool_addresses.insert(pool.address.as_str()) {
            bail!(
                "duplicate pool address in opportunity universe: {}",
                pool.address
            );
        }
    }

    let affected = affected_pool_addresses
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    for address in &affected {
        if !pool_addresses.contains(address) {
            bail!("affected pool is outside opportunity universe: {address}");
        }
    }

    let token_mints = pools
        .iter()
        .map(|pool| token_mint_for_base(pool, base_mint))
        .collect::<Result<Vec<_>>>()?;
    let mut routes = Vec::new();
    let mut seen = HashSet::new();
    for first in 0..pools.len() {
        for second in 0..pools.len() {
            if first == second || token_mints[first] != token_mints[second] {
                continue;
            }
            if !affected.contains(pools[first].address.as_str())
                && !affected.contains(pools[second].address.as_str())
            {
                continue;
            }
            let key = (
                pools[first].address.as_str(),
                pools[second].address.as_str(),
            );
            if seen.insert(key) {
                routes.push(DirectedPoolRoute {
                    token_mint: token_mints[first].to_owned(),
                    first_pool: pools[first].clone(),
                    second_pool: pools[second].clone(),
                });
            }
        }
    }
    Ok(routes)
}

fn token_mint_for_base<'a>(pool: &'a PoolInfo, base_mint: &str) -> Result<&'a str> {
    match (pool.mint_a == base_mint, pool.mint_b == base_mint) {
        (true, false) => Ok(pool.mint_b.as_str()),
        (false, true) => Ok(pool.mint_a.as_str()),
        _ => bail!(
            "pool is not a valid single-base pair for affected-route engine: {}",
            pool.address
        ),
    }
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
/// 这里的 gross profit 还没有扣 Priority Fee / Jito Tip 等执行成本。
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
    let gross_return_bps = scaled_return(gross_profit_raw, first_leg.amount_in, 10_000)?;
    let gross_return_ppm = scaled_return(gross_profit_raw, first_leg.amount_in, 1_000_000)?;

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
        gross_return_ppm,
        oldest_slot: first_leg.snapshot_slot.min(second_leg.snapshot_slot),
        newest_slot: first_leg.snapshot_slot.max(second_leg.snapshot_slot),
    })
}

/// 把独立的执行成本模型应用到已经验证过的两腿闭环。
/// 成本以 lamports 计，因此只适用于当前以 WSOL/SOL 为 base asset 的研究路径。
pub fn apply_execution_cost(
    round_trip: &RoundTripOpportunity,
    execution_cost: ExecutionCost,
) -> Result<NetOpportunity> {
    let execution_cost_lamports = execution_cost.total_lamports()?;
    let net_profit_raw = round_trip
        .gross_profit_raw
        .checked_sub(i128::from(execution_cost_lamports))
        .context("net profit overflow")?;
    let net_return_ppm = scaled_return(net_profit_raw, round_trip.input_amount, 1_000_000)?;

    Ok(NetOpportunity {
        round_trip: round_trip.clone(),
        execution_cost,
        execution_cost_lamports,
        net_profit_raw,
        net_return_ppm,
    })
}

/// 同一路径的多金额曲线按索引成对验算。每个点仍复用 `evaluate_round_trip` 的
/// Mint、金额连续性与不同 Pool 校验，不允许批量接口绕过单点安全条件。
pub fn evaluate_round_trip_curve(
    first_legs: &[SwapQuote],
    second_legs: &[SwapQuote],
) -> Result<Vec<RoundTripOpportunity>> {
    if first_legs.is_empty() {
        bail!("round-trip curve must contain at least one point");
    }
    if first_legs.len() != second_legs.len() {
        bail!("round-trip curve leg count mismatch");
    }

    first_legs
        .iter()
        .zip(second_legs)
        .map(|(first, second)| evaluate_round_trip(first, second))
        .collect()
}

fn scaled_return(profit_raw: i128, input_amount: u64, scale: i128) -> Result<i128> {
    if input_amount == 0 {
        bail!("return input amount must be positive");
    }
    profit_raw
        .checked_mul(scale)
        .map(|value| value / i128::from(input_amount))
        .context("scaled return overflow")
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

    fn profitable_round_trip() -> RoundTripOpportunity {
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
        evaluate_round_trip(&first, &second).unwrap()
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
        let opportunity = profitable_round_trip();
        assert_eq!(opportunity.base_mint, "SOL");
        assert_eq!(opportunity.intermediate_mint, "TOKEN");
        assert_eq!(opportunity.input_amount, 1_000_000);
        assert_eq!(opportunity.intermediate_amount, 2_000_000);
        assert_eq!(opportunity.final_amount, 1_010_000);
        assert_eq!(opportunity.gross_profit_raw, 10_000);
        assert_eq!(opportunity.gross_return_bps, 100);
        assert_eq!(opportunity.gross_return_ppm, 10_000);
        assert_eq!(opportunity.oldest_slot, 100);
        assert_eq!(opportunity.newest_slot, 103);
    }

    #[test]
    fn ppm_preserves_sub_basis_point_sign() {
        let first = quote(Dex::Raydium, "a", "SOL", "TOKEN", 10_000_000, 20_000_000, 1);
        let second = quote(Dex::Orca, "b", "TOKEN", "SOL", 20_000_000, 9_999_880, 1);

        let opportunity = evaluate_round_trip(&first, &second).unwrap();
        assert_eq!(opportunity.gross_profit_raw, -120);
        assert_eq!(opportunity.gross_return_bps, 0);
        assert_eq!(opportunity.gross_return_ppm, -12);
    }

    #[test]
    fn loss_is_signed_instead_of_underflowing() {
        let first = quote(Dex::Raydium, "a", "SOL", "TOKEN", 1_000, 2_000, 10);
        let second = quote(Dex::Orca, "b", "TOKEN", "SOL", 2_000, 990, 9);

        let opportunity = evaluate_round_trip(&first, &second).unwrap();
        assert_eq!(opportunity.gross_profit_raw, -10);
        assert_eq!(opportunity.gross_return_bps, -100);
        assert_eq!(opportunity.gross_return_ppm, -10_000);
        assert_eq!(opportunity.oldest_slot, 9);
        assert_eq!(opportunity.newest_slot, 10);
    }

    #[test]
    fn execution_cost_totals_components_and_detects_overflow() {
        let cost = ExecutionCost {
            base_fee_lamports: 5_000,
            priority_fee_lamports: 2_000,
            jito_tip_lamports: 1_000,
            other_lamports: 500,
        };
        assert_eq!(cost.total_lamports().unwrap(), 8_500);

        let overflow = ExecutionCost {
            base_fee_lamports: u64::MAX,
            priority_fee_lamports: 1,
            jito_tip_lamports: 0,
            other_lamports: 0,
        };
        assert!(overflow.total_lamports().is_err());
    }

    #[test]
    fn zero_execution_cost_preserves_gross_profit() {
        let gross = profitable_round_trip();
        let net = apply_execution_cost(&gross, ExecutionCost::ZERO).unwrap();
        assert_eq!(net.execution_cost_lamports, 0);
        assert_eq!(net.net_profit_raw, gross.gross_profit_raw);
        assert_eq!(net.net_return_ppm, gross.gross_return_ppm);
        assert!(net.is_profitable());
    }

    #[test]
    fn execution_cost_can_turn_gross_profit_into_net_loss() {
        let gross = profitable_round_trip();
        let cost = ExecutionCost {
            base_fee_lamports: 5_000,
            priority_fee_lamports: 3_000,
            jito_tip_lamports: 4_000,
            other_lamports: 0,
        };
        let net = apply_execution_cost(&gross, cost).unwrap();
        assert_eq!(net.execution_cost_lamports, 12_000);
        assert_eq!(net.net_profit_raw, -2_000);
        assert_eq!(net.net_return_ppm, -2_000);
        assert!(!net.is_profitable());
    }

    #[test]
    fn execution_cost_makes_existing_loss_more_negative() {
        let first = quote(Dex::Raydium, "a", "SOL", "TOKEN", 1_000_000, 2_000_000, 1);
        let second = quote(Dex::Orca, "b", "TOKEN", "SOL", 2_000_000, 990_000, 2);
        let gross = evaluate_round_trip(&first, &second).unwrap();
        let net = apply_execution_cost(
            &gross,
            ExecutionCost {
                base_fee_lamports: 5_000,
                priority_fee_lamports: 0,
                jito_tip_lamports: 1_000,
                other_lamports: 0,
            },
        )
        .unwrap();
        assert_eq!(gross.gross_profit_raw, -10_000);
        assert_eq!(net.net_profit_raw, -16_000);
        assert_eq!(net.net_return_ppm, -16_000);
    }

    #[test]
    fn evaluates_curve_and_rejects_empty_or_mismatched_batches() {
        let first = vec![
            quote(Dex::Raydium, "a", "SOL", "TOKEN", 100, 200, 1),
            quote(Dex::Raydium, "a", "SOL", "TOKEN", 200, 390, 1),
        ];
        let second = vec![
            quote(Dex::Orca, "b", "TOKEN", "SOL", 200, 101, 2),
            quote(Dex::Orca, "b", "TOKEN", "SOL", 390, 198, 2),
        ];

        let curve = evaluate_round_trip_curve(&first, &second).unwrap();
        assert_eq!(curve.len(), 2);
        assert_eq!(curve[0].gross_profit_raw, 1);
        assert_eq!(curve[1].gross_profit_raw, -2);
        assert!(evaluate_round_trip_curve(&[], &[]).is_err());
        assert!(evaluate_round_trip_curve(&first, &second[..1]).is_err());
    }

    #[test]
    fn curve_still_rejects_a_broken_point() {
        let first = vec![quote(Dex::Raydium, "a", "SOL", "TOKEN", 100, 200, 1)];
        let second = vec![quote(Dex::Orca, "b", "TOKEN", "SOL", 199, 101, 2)];
        assert!(evaluate_round_trip_curve(&first, &second).is_err());
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

    fn route_pool(dex: Dex, address: &str, token: &str) -> PoolInfo {
        PoolInfo {
            dex,
            address: address.into(),
            pool_type: "test".into(),
            program_id: Some("program".into()),
            mint_a: token.into(),
            mint_b: "SOL".into(),
            tvl_usd: 1_000.0,
        }
    }

    #[test]
    fn affected_routes_only_cover_the_updated_tokens_group() {
        let pools = vec![
            route_pool(Dex::Raydium, "a", "TOKEN1"),
            route_pool(Dex::Orca, "b", "TOKEN1"),
            route_pool(Dex::MeteoraDlmm, "c", "TOKEN1"),
            route_pool(Dex::Raydium, "d", "TOKEN2"),
            route_pool(Dex::Orca, "e", "TOKEN2"),
            route_pool(Dex::MeteoraDlmm, "f", "TOKEN2"),
        ];
        let routes = affected_directed_pool_routes(&pools, &["b".into()], "SOL").unwrap();
        let pairs = routes
            .iter()
            .map(|route| {
                (
                    route.first_pool.address.as_str(),
                    route.second_pool.address.as_str(),
                )
            })
            .collect::<Vec<_>>();
        assert_eq!(pairs, vec![("a", "b"), ("b", "a"), ("b", "c"), ("c", "b")]);
        assert!(routes.iter().all(|route| route.token_mint == "TOKEN1"));
    }

    #[test]
    fn multiple_affected_pools_expand_to_all_related_routes_without_duplicates() {
        let pools = vec![
            route_pool(Dex::Raydium, "a", "TOKEN1"),
            route_pool(Dex::Orca, "b", "TOKEN1"),
            route_pool(Dex::MeteoraDlmm, "c", "TOKEN1"),
        ];
        let routes =
            affected_directed_pool_routes(&pools, &["b".into(), "c".into(), "b".into()], "SOL")
                .unwrap();
        assert_eq!(routes.len(), 6);
        let pairs = routes
            .iter()
            .map(|route| {
                (
                    route.first_pool.address.clone(),
                    route.second_pool.address.clone(),
                )
            })
            .collect::<HashSet<_>>();
        assert_eq!(pairs.len(), 6);
    }

    #[test]
    fn affected_routes_reject_unknown_pool_and_invalid_base_universe() {
        let pools = vec![
            route_pool(Dex::Raydium, "a", "TOKEN1"),
            route_pool(Dex::Orca, "b", "TOKEN1"),
        ];
        assert!(affected_directed_pool_routes(&pools, &["missing".into()], "SOL").is_err());

        let mut invalid = pools;
        invalid[1].mint_b = "OTHER".into();
        assert!(affected_directed_pool_routes(&invalid, &["a".into()], "SOL").is_err());
    }

    #[test]
    fn opportunity_event_preserves_evaluated_net_result() {
        let gross = profitable_round_trip();
        let net = apply_execution_cost(
            &gross,
            ExecutionCost {
                base_fee_lamports: 5_000,
                priority_fee_lamports: 0,
                jito_tip_lamports: 1_000,
                other_lamports: 0,
            },
        )
        .unwrap();
        let route = DirectedPoolRoute {
            token_mint: "TOKEN".into(),
            first_pool: route_pool(Dex::Orca, "orca", "TOKEN"),
            second_pool: route_pool(Dex::Raydium, "raydium", "TOKEN"),
        };
        let event = OpportunityEvent::evaluated(&route, &net).unwrap();
        assert_eq!(event.input_amount, 1_000_000);
        assert_eq!(event.first_pool, "orca");
        assert_eq!(event.second_pool, "raydium");
        assert_eq!(
            event.outcome,
            OpportunityEventOutcome::Evaluated {
                intermediate_amount: 2_000_000,
                final_amount: 1_010_000,
                gross_profit_raw: 10_000,
                gross_return_ppm: 10_000,
                execution_cost_lamports: 6_000,
                net_profit_raw: 4_000,
                net_return_ppm: 4_000,
                oldest_slot: 100,
                newest_slot: 103,
            }
        );
    }

    #[test]
    fn opportunity_event_keeps_liquidity_failure_as_explicit_state() {
        let route = DirectedPoolRoute {
            token_mint: "TOKEN".into(),
            first_pool: route_pool(Dex::Orca, "orca", "TOKEN"),
            second_pool: route_pool(Dex::Raydium, "raydium", "TOKEN"),
        };
        let event = OpportunityEvent::insufficient_liquidity(
            &route,
            100_000_000,
            LiquidityStage::SecondLeg,
        )
        .unwrap();
        assert_eq!(event.input_amount, 100_000_000);
        assert_eq!(
            event.outcome,
            OpportunityEventOutcome::InsufficientLiquidity {
                stage: LiquidityStage::SecondLeg
            }
        );
        assert!(
            OpportunityEvent::insufficient_liquidity(&route, 0, LiquidityStage::FirstLeg).is_err()
        );
    }
}
