use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dex {
    Raydium,
    Orca,
    MeteoraDlmm,
    MeteoraDammV2,
}

impl fmt::Display for Dex {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            Self::Raydium => "Raydium",
            Self::Orca => "Orca",
            Self::MeteoraDlmm => "Meteora DLMM",
            Self::MeteoraDammV2 => "Meteora DAMM v2",
        };
        write!(f, "{name}")
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PoolInfo {
    pub dex: Dex,
    pub address: String,
    pub pool_type: String,
    pub program_id: Option<String>,
    pub mint_a: String,
    pub mint_b: String,
    pub tvl_usd: f64,
}

impl PoolInfo {
    /// 交易对匹配不依赖池内 token A/B 的排列顺序。
    pub fn matches_pair(&self, mint_x: &str, mint_y: &str) -> bool {
        (self.mint_a == mint_x && self.mint_b == mint_y)
            || (self.mint_a == mint_y && self.mint_b == mint_x)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_pool() -> PoolInfo {
        PoolInfo {
            dex: Dex::Raydium,
            address: "pool".into(),
            pool_type: "Concentrated".into(),
            program_id: None,
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd: 100.0,
        }
    }

    #[test]
    fn matches_pair_accepts_both_mint_orders() {
        let pool = sample_pool();
        assert!(pool.matches_pair("A", "B"));
        assert!(pool.matches_pair("B", "A"));
    }

    #[test]
    fn matches_pair_rejects_different_pair() {
        let pool = sample_pool();
        assert!(!pool.matches_pair("A", "C"));
    }
}
