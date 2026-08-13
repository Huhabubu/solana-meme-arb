#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Token {
    pub symbol: &'static str,
    pub mint: &'static str,
}

pub const WSOL: &str = "So11111111111111111111111111111111111111112";

const TRACKED_TOKENS: [Token; 2] = [
    Token {
        symbol: "BONK",
        mint: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    },
    Token {
        symbol: "WIF",
        mint: "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    },
];

pub fn tracked_tokens() -> &'static [Token] {
    &TRACKED_TOKENS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tracked_tokens_are_non_empty_and_unique() {
        let tokens = tracked_tokens();
        assert!(!tokens.is_empty());

        for (index, token) in tokens.iter().enumerate() {
            assert!(!token.symbol.is_empty());
            assert!(!token.mint.is_empty());
            assert_ne!(token.mint, WSOL);

            for other in tokens.iter().skip(index + 1) {
                assert_ne!(token.mint, other.mint);
            }
        }
    }
}
