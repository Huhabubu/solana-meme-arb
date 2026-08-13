const APP_NAME: &str = "solana-meme-arb";

fn main() {
    println!("{APP_NAME}: bootstrap ready");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_name_is_stable() {
        assert_eq!(APP_NAME, "solana-meme-arb");
    }
}
