use anyhow::{bail, Result};
use serde_json::Value;

/// DEX API 对 TVL 的编码并不统一：有的返回 JSON number，有的返回字符串。
pub fn number_from_value(value: &Value) -> Result<f64> {
    if let Some(number) = value.as_f64() {
        return Ok(number);
    }

    if let Some(text) = value.as_str() {
        return Ok(text.parse::<f64>()?);
    }

    bail!("expected numeric JSON value, got {value}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_number_and_numeric_string() {
        assert_eq!(number_from_value(&serde_json::json!(12.5)).unwrap(), 12.5);
        assert_eq!(number_from_value(&serde_json::json!("12.5")).unwrap(), 12.5);
    }

    #[test]
    fn rejects_non_numeric_value() {
        assert!(number_from_value(&serde_json::json!(true)).is_err());
        assert!(number_from_value(&serde_json::json!("abc")).is_err());
    }
}
