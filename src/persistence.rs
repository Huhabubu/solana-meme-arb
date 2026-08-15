use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, BufWriter, Seek, SeekFrom, Write},
    path::Path,
};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::opportunity::{LiquidityStage, OpportunityEvent, OpportunityEventOutcome};

pub const OPPORTUNITY_RECORD_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OpportunityRecordStatus {
    Evaluated,
    InsufficientLiquidity,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpportunityRecord {
    pub schema_version: u32,
    pub observed_at_unix_ms: u64,
    pub trigger_slot: u64,
    pub trigger_account: String,
    pub trigger_subscription_id: u64,
    pub token_mint: String,
    pub first_dex: String,
    pub first_pool: String,
    pub second_dex: String,
    pub second_pool: String,
    pub input_amount: u64,
    pub status: OpportunityRecordStatus,
    pub liquidity_stage: Option<String>,
    pub intermediate_amount: Option<u64>,
    pub final_amount: Option<u64>,
    pub gross_profit_raw: Option<i128>,
    pub gross_return_ppm: Option<i128>,
    pub execution_cost_lamports: Option<u64>,
    pub net_profit_raw: Option<i128>,
    pub net_return_ppm: Option<i128>,
    pub oldest_slot: Option<u64>,
    pub newest_slot: Option<u64>,
}

impl OpportunityRecord {
    pub fn from_event(
        event: &OpportunityEvent,
        observed_at_unix_ms: u64,
        trigger_slot: u64,
        trigger_account: &str,
        trigger_subscription_id: u64,
    ) -> Result<Self> {
        if observed_at_unix_ms == 0 {
            bail!("opportunity record observation time must be positive");
        }
        if trigger_account.trim().is_empty() {
            bail!("opportunity record trigger account cannot be empty");
        }

        let (
            status,
            liquidity_stage,
            intermediate_amount,
            final_amount,
            gross_profit_raw,
            gross_return_ppm,
            execution_cost_lamports,
            net_profit_raw,
            net_return_ppm,
            oldest_slot,
            newest_slot,
        ) = match event.outcome {
            OpportunityEventOutcome::Evaluated {
                intermediate_amount,
                final_amount,
                gross_profit_raw,
                gross_return_ppm,
                execution_cost_lamports,
                net_profit_raw,
                net_return_ppm,
                oldest_slot,
                newest_slot,
            } => (
                OpportunityRecordStatus::Evaluated,
                None,
                Some(intermediate_amount),
                Some(final_amount),
                Some(gross_profit_raw),
                Some(gross_return_ppm),
                Some(execution_cost_lamports),
                Some(net_profit_raw),
                Some(net_return_ppm),
                Some(oldest_slot),
                Some(newest_slot),
            ),
            OpportunityEventOutcome::InsufficientLiquidity { stage } => (
                OpportunityRecordStatus::InsufficientLiquidity,
                Some(match stage {
                    LiquidityStage::FirstLeg => "first_leg".to_owned(),
                    LiquidityStage::SecondLeg => "second_leg".to_owned(),
                }),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        };

        let record = Self {
            schema_version: OPPORTUNITY_RECORD_SCHEMA_VERSION,
            observed_at_unix_ms,
            trigger_slot,
            trigger_account: trigger_account.to_owned(),
            trigger_subscription_id,
            token_mint: event.token_mint.clone(),
            first_dex: event.first_dex.to_string(),
            first_pool: event.first_pool.clone(),
            second_dex: event.second_dex.to_string(),
            second_pool: event.second_pool.clone(),
            input_amount: event.input_amount,
            status,
            liquidity_stage,
            intermediate_amount,
            final_amount,
            gross_profit_raw,
            gross_return_ppm,
            execution_cost_lamports,
            net_profit_raw,
            net_return_ppm,
            oldest_slot,
            newest_slot,
        };
        record.validate()?;
        Ok(record)
    }

    fn validate(&self) -> Result<()> {
        if self.schema_version != OPPORTUNITY_RECORD_SCHEMA_VERSION {
            bail!(
                "unsupported opportunity record schema version: {}",
                self.schema_version
            );
        }
        if self.observed_at_unix_ms == 0
            || self.trigger_account.trim().is_empty()
            || self.token_mint.trim().is_empty()
            || self.first_dex.trim().is_empty()
            || self.first_pool.trim().is_empty()
            || self.second_dex.trim().is_empty()
            || self.second_pool.trim().is_empty()
            || self.input_amount == 0
        {
            bail!("opportunity record contains an empty required field");
        }

        let evaluated_fields = [
            self.intermediate_amount.is_some(),
            self.final_amount.is_some(),
            self.gross_profit_raw.is_some(),
            self.gross_return_ppm.is_some(),
            self.execution_cost_lamports.is_some(),
            self.net_profit_raw.is_some(),
            self.net_return_ppm.is_some(),
            self.oldest_slot.is_some(),
            self.newest_slot.is_some(),
        ];
        match self.status {
            OpportunityRecordStatus::Evaluated => {
                if self.liquidity_stage.is_some() || evaluated_fields.iter().any(|present| !present)
                {
                    bail!("evaluated opportunity record has inconsistent optional fields");
                }
            }
            OpportunityRecordStatus::InsufficientLiquidity => {
                if !matches!(
                    self.liquidity_stage.as_deref(),
                    Some("first_leg" | "second_leg")
                ) || evaluated_fields.iter().any(|present| *present)
                {
                    bail!("insufficient-liquidity record has inconsistent optional fields");
                }
            }
        }
        Ok(())
    }
}

pub fn append_records(path: &Path, records: &[OpportunityRecord]) -> Result<()> {
    if records.is_empty() {
        bail!("cannot append an empty opportunity record batch");
    }
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent).with_context(|| {
            format!(
                "failed to create opportunity log directory: {}",
                parent.display()
            )
        })?;
    }
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("failed to open opportunity log: {}", path.display()))?;
    let mut writer = BufWriter::new(file);
    for record in records {
        record.validate()?;
        serde_json::to_writer(&mut writer, record)
            .context("failed to serialize opportunity record")?;
        writer
            .write_all(b"\n")
            .context("failed to terminate opportunity JSONL record")?;
    }
    writer
        .flush()
        .context("failed to flush opportunity JSONL log")?;
    writer
        .get_ref()
        .sync_data()
        .context("failed to sync opportunity JSONL log")?;
    Ok(())
}

/// 流式校验并汇总 JSONL，内存只与单行和分组数量相关。
/// 如果进程崩溃只留下最后一行的不完整 JSON，则截断该尾部；中间损坏仍直接失败。
pub fn scan_records(path: &Path) -> Result<OpportunityStats> {
    let file = File::open(path)
        .with_context(|| format!("failed to open opportunity log: {}", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut stats = OpportunityStats::default();
    let mut line = Vec::new();
    let mut valid_bytes = 0u64;
    let mut line_number = 0usize;

    loop {
        line.clear();
        let bytes_read = reader.read_until(b'\n', &mut line).with_context(|| {
            format!("failed to read opportunity JSONL line {}", line_number + 1)
        })?;
        if bytes_read == 0 {
            break;
        }
        line_number += 1;
        let terminated = line.ends_with(b"\n");
        let mut json_end = line.len() - usize::from(terminated);
        if json_end > 0 && line[json_end - 1] == b'\r' {
            json_end -= 1;
        }
        let json = &line[..json_end];

        if json.iter().all(u8::is_ascii_whitespace) {
            if !terminated {
                drop(reader);
                repair_log_tail(path, valid_bytes, false)?;
                return Ok(stats);
            }
            bail!("opportunity JSONL contains blank line {line_number}");
        }

        let record = match serde_json::from_slice::<OpportunityRecord>(json) {
            Ok(record) => record,
            Err(_) if !terminated => {
                drop(reader);
                repair_log_tail(path, valid_bytes, false)?;
                return Ok(stats);
            }
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("invalid opportunity JSONL line {line_number}"));
            }
        };
        record.validate().with_context(|| {
            format!("invalid opportunity record semantics at line {line_number}")
        })?;
        stats.ingest_record(&record)?;
        valid_bytes = valid_bytes
            .checked_add(u64::try_from(bytes_read).context("opportunity log offset overflow")?)
            .context("opportunity log offset overflow")?;

        if !terminated {
            drop(reader);
            repair_log_tail(path, valid_bytes, true)?;
            return Ok(stats);
        }
    }

    Ok(stats)
}

fn repair_log_tail(path: &Path, valid_bytes: u64, append_newline: bool) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .open(path)
        .with_context(|| format!("failed to repair opportunity log: {}", path.display()))?;
    file.set_len(valid_bytes)
        .context("failed to truncate incomplete opportunity JSONL tail")?;
    if append_newline {
        file.seek(SeekFrom::End(0))
            .context("failed to seek opportunity JSONL tail")?;
        file.write_all(b"\n")
            .context("failed to terminate final opportunity JSONL record")?;
    }
    file.sync_data()
        .context("failed to sync repaired opportunity JSONL log")?;
    Ok(())
}

#[cfg(test)]
fn read_records(path: &Path) -> Result<Vec<OpportunityRecord>> {
    let file = File::open(path)
        .with_context(|| format!("failed to open opportunity log: {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut records = Vec::new();
    for (index, line) in reader.lines().enumerate() {
        let line =
            line.with_context(|| format!("failed to read opportunity JSONL line {}", index + 1))?;
        if line.trim().is_empty() {
            bail!("opportunity JSONL contains blank line {}", index + 1);
        }
        let record: OpportunityRecord = serde_json::from_str(&line)
            .with_context(|| format!("invalid opportunity JSONL line {}", index + 1))?;
        record.validate().with_context(|| {
            format!("invalid opportunity record semantics at line {}", index + 1)
        })?;
        records.push(record);
    }
    Ok(records)
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct OpportunityGroupKey {
    pub token_mint: String,
    pub first_dex: String,
    pub first_pool: String,
    pub second_dex: String,
    pub second_pool: String,
    pub input_amount: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct OpportunityGroupStats {
    pub total: u64,
    pub evaluated: u64,
    pub insufficient_liquidity: u64,
    pub gross_positive: u64,
    pub net_positive: u64,
    pub best_net_profit_raw: Option<i128>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct OpportunityStats {
    pub total: u64,
    pub evaluated: u64,
    pub insufficient_liquidity: u64,
    pub gross_positive: u64,
    pub net_positive: u64,
    pub best_net_profit_raw: Option<i128>,
    pub groups: BTreeMap<OpportunityGroupKey, OpportunityGroupStats>,
}

impl OpportunityStats {
    pub fn ingest_record(&mut self, record: &OpportunityRecord) -> Result<()> {
        record.validate()?;
        let key = OpportunityGroupKey {
            token_mint: record.token_mint.clone(),
            first_dex: record.first_dex.clone(),
            first_pool: record.first_pool.clone(),
            second_dex: record.second_dex.clone(),
            second_pool: record.second_pool.clone(),
            input_amount: record.input_amount,
        };
        let group = self.groups.entry(key).or_default();
        self.total += 1;
        group.total += 1;

        match record.status {
            OpportunityRecordStatus::Evaluated => {
                let gross_profit = record
                    .gross_profit_raw
                    .context("validated evaluated record lost gross profit")?;
                let net_profit = record
                    .net_profit_raw
                    .context("validated evaluated record lost net profit")?;
                self.evaluated += 1;
                group.evaluated += 1;
                if gross_profit > 0 {
                    self.gross_positive += 1;
                    group.gross_positive += 1;
                }
                if net_profit > 0 {
                    self.net_positive += 1;
                    group.net_positive += 1;
                }
                update_best(&mut self.best_net_profit_raw, net_profit);
                update_best(&mut group.best_net_profit_raw, net_profit);
            }
            OpportunityRecordStatus::InsufficientLiquidity => {
                self.insufficient_liquidity += 1;
                group.insufficient_liquidity += 1;
            }
        }
        Ok(())
    }

    pub fn ingest_records(&mut self, records: &[OpportunityRecord]) -> Result<()> {
        for record in records {
            self.ingest_record(record)?;
        }
        Ok(())
    }
}

#[cfg(test)]
fn summarize_records(records: &[OpportunityRecord]) -> Result<OpportunityStats> {
    let mut stats = OpportunityStats::default();
    stats.ingest_records(records)?;
    Ok(stats)
}

fn update_best(current: &mut Option<i128>, candidate: i128) {
    if current.is_none_or(|value| candidate > value) {
        *current = Some(candidate);
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    use super::*;
    use crate::{model::Dex, opportunity::OpportunityEventOutcome};

    fn evaluated_event(net_profit: i128, gross_profit: i128) -> OpportunityEvent {
        OpportunityEvent {
            token_mint: "TOKEN".into(),
            first_dex: Dex::Raydium,
            first_pool: "pool-a".into(),
            second_dex: Dex::Orca,
            second_pool: "pool-b".into(),
            input_amount: 10_000_000,
            outcome: OpportunityEventOutcome::Evaluated {
                intermediate_amount: 20_000_000,
                final_amount: 10_000_100,
                gross_profit_raw: gross_profit,
                gross_return_ppm: 10,
                execution_cost_lamports: 6_000,
                net_profit_raw: net_profit,
                net_return_ppm: 4,
                oldest_slot: 100,
                newest_slot: 101,
            },
        }
    }

    fn insufficient_event() -> OpportunityEvent {
        OpportunityEvent {
            token_mint: "TOKEN".into(),
            first_dex: Dex::Raydium,
            first_pool: "pool-a".into(),
            second_dex: Dex::Orca,
            second_pool: "pool-b".into(),
            input_amount: 50_000_000,
            outcome: OpportunityEventOutcome::InsufficientLiquidity {
                stage: LiquidityStage::SecondLeg,
            },
        }
    }

    fn record(event: &OpportunityEvent) -> OpportunityRecord {
        OpportunityRecord::from_event(event, 1_000, 99, "trigger", 7).unwrap()
    }

    fn temp_path(name: &str) -> std::path::PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "solana-meme-arb-{name}-{}-{nanos}.jsonl",
            std::process::id()
        ))
    }

    #[test]
    fn event_conversion_preserves_evaluated_and_liquidity_states() {
        let evaluated = record(&evaluated_event(4_000, 10_000));
        assert_eq!(evaluated.status, OpportunityRecordStatus::Evaluated);
        assert_eq!(evaluated.net_profit_raw, Some(4_000));
        assert_eq!(evaluated.liquidity_stage, None);

        let insufficient = record(&insufficient_event());
        assert_eq!(
            insufficient.status,
            OpportunityRecordStatus::InsufficientLiquidity
        );
        assert_eq!(insufficient.liquidity_stage.as_deref(), Some("second_leg"));
        assert_eq!(insufficient.net_profit_raw, None);
    }

    #[test]
    fn jsonl_append_is_additive_and_round_trips() {
        let path = temp_path("append");
        let first = record(&evaluated_event(4_000, 10_000));
        let second = record(&insufficient_event());
        append_records(&path, std::slice::from_ref(&first)).unwrap();
        append_records(&path, std::slice::from_ref(&second)).unwrap();
        let loaded = read_records(&path).unwrap();
        assert_eq!(loaded, vec![first, second]);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn malformed_jsonl_is_reported_instead_of_skipped() {
        let path = temp_path("bad");
        fs::write(&path, b"{not-json}\n").unwrap();
        assert!(read_records(&path).is_err());
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn streaming_scan_matches_batch_summary() {
        let path = temp_path("scan");
        let records = vec![
            record(&evaluated_event(4_000, 10_000)),
            record(&insufficient_event()),
            record(&evaluated_event(-2_000, -1_000)),
        ];
        append_records(&path, &records).unwrap();
        assert_eq!(
            scan_records(&path).unwrap(),
            summarize_records(&records).unwrap()
        );
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn streaming_scan_truncates_only_an_unterminated_invalid_tail() {
        let path = temp_path("truncated-tail");
        let first = record(&evaluated_event(4_000, 10_000));
        append_records(&path, std::slice::from_ref(&first)).unwrap();
        OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"{\"schema_version\":")
            .unwrap();

        let stats = scan_records(&path).unwrap();
        assert_eq!(stats.total, 1);
        assert_eq!(read_records(&path).unwrap(), vec![first]);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn streaming_scan_rejects_terminated_corruption() {
        let path = temp_path("interior-corruption");
        let first = record(&evaluated_event(4_000, 10_000));
        append_records(&path, std::slice::from_ref(&first)).unwrap();
        OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"{not-json}\n")
            .unwrap();

        assert!(scan_records(&path).is_err());
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn summary_counts_profitability_liquidity_and_route_amount_groups() {
        let records = vec![
            record(&evaluated_event(4_000, 10_000)),
            record(&evaluated_event(-2_000, -1_000)),
            record(&insufficient_event()),
        ];
        let stats = summarize_records(&records).unwrap();
        assert_eq!(stats.total, 3);
        assert_eq!(stats.evaluated, 2);
        assert_eq!(stats.insufficient_liquidity, 1);
        assert_eq!(stats.gross_positive, 1);
        assert_eq!(stats.net_positive, 1);
        assert_eq!(stats.best_net_profit_raw, Some(4_000));
        assert_eq!(stats.groups.len(), 2);
        let ten_million = stats
            .groups
            .iter()
            .find(|(key, _)| key.input_amount == 10_000_000)
            .unwrap()
            .1;
        assert_eq!(ten_million.total, 2);
        assert_eq!(ten_million.evaluated, 2);
        assert_eq!(ten_million.net_positive, 1);
    }

    #[test]
    fn incremental_statistics_match_batch_summary() {
        let records = vec![
            record(&evaluated_event(4_000, 10_000)),
            record(&insufficient_event()),
            record(&evaluated_event(-2_000, -1_000)),
        ];
        let batch = summarize_records(&records).unwrap();
        let mut incremental = OpportunityStats::default();
        incremental.ingest_records(&records[..2]).unwrap();
        incremental.ingest_records(&records[2..]).unwrap();
        assert_eq!(incremental, batch);
    }
}
