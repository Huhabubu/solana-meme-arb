use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs::{create_dir_all, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{bail, Context, Result};
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::{net::TcpStream, time::timeout};
use tokio_tungstenite::{connect_async, tungstenite::Message, MaybeTlsStream, WebSocketStream};

use crate::{
    config::HeliusConfig,
    dex::{
        meteora::DLMM_PROGRAM_ID,
        orca_whirlpool::ORCA_WHIRLPOOL_PROGRAM_ID,
        raydium_amm::RAYDIUM_AMM_V4_PROGRAM_ID,
    },
    discovery::{
        discover_pair, select_monitoring_candidates, MAX_POOLS_PER_DEX, MIN_MONITOR_TVL_USD,
    },
    model::{Dex, PoolInfo},
    tokens::WSOL,
};

const APP_NAME: &str = "solana-meme-arb-event-monitor";
const SUBSCRIPTION_COMMITMENT: &str = "confirmed";
const DEFAULT_UPDATE_TIMEOUT_SECONDS: u64 = 60;
const DEFAULT_MAX_RECONNECTS: usize = 20;
const DEFAULT_POOL_CACHE_TTL_SECONDS: u64 = 300;
const DEFAULT_PARSE_RETRIES: usize = 5;
const SIGNATURE_DEDUP_CAPACITY: usize = 20_000;
const EVENT_SCHEMA: &str = "event-driven-v1";

#[derive(Debug, Clone, PartialEq, Eq)]
struct RawProgramTransaction {
    signature: String,
    slot: u64,
    program_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum DirectSwapDirection {
    WsolToToken,
    TokenToWsol,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DirectSwapEvent {
    signature: String,
    slot: u64,
    trigger_program: String,
    source: String,
    direction: DirectSwapDirection,
    token_mint: String,
    wsol_amount_lamports: Option<u64>,
    token_amount_raw: Option<u64>,
}

#[derive(Debug, Serialize)]
struct EventPoolRecord {
    dex: String,
    address: String,
    pool_type: String,
    program_id: Option<String>,
    mint_a: String,
    mint_b: String,
    tvl_usd: f64,
}

impl From<&PoolInfo> for EventPoolRecord {
    fn from(pool: &PoolInfo) -> Self {
        Self {
            dex: pool.dex.to_string(),
            address: pool.address.clone(),
            pool_type: pool.pool_type.clone(),
            program_id: pool.program_id.clone(),
            mint_a: pool.mint_a.clone(),
            mint_b: pool.mint_b.clone(),
            tvl_usd: if pool.tvl_usd.is_finite() {
                pool.tvl_usd
            } else {
                0.0
            },
        }
    }
}

#[derive(Debug, Serialize)]
struct EventRecord {
    schema: &'static str,
    observed_at_unix_ms: u64,
    signature: String,
    slot: u64,
    trigger_program: String,
    source: String,
    direction: DirectSwapDirection,
    token_mint: String,
    wsol_amount_lamports: Option<u64>,
    token_amount_raw: Option<u64>,
    pool_discovery_ok: bool,
    pool_discovery_ms: u64,
    candidate_pool_count: usize,
    candidate_pools: Vec<EventPoolRecord>,
}

#[derive(Debug)]
struct EventMonitorConfig {
    max_events: Option<usize>,
    max_seconds: Option<u64>,
    update_timeout: Duration,
    max_reconnects: usize,
    pool_cache_ttl: Duration,
    min_wsol_lamports: u64,
    parse_retries: usize,
}

impl EventMonitorConfig {
    fn from_env() -> Result<Self> {
        Ok(Self {
            max_events: parse_optional_usize("EVENT_MONITOR_MAX_EVENTS")?,
            max_seconds: parse_optional_u64("EVENT_MONITOR_MAX_SECONDS")?,
            update_timeout: Duration::from_secs(parse_u64_with_default(
                "EVENT_MONITOR_UPDATE_TIMEOUT_SECONDS",
                DEFAULT_UPDATE_TIMEOUT_SECONDS,
            )?),
            max_reconnects: parse_usize_with_default(
                "EVENT_MONITOR_MAX_RECONNECTS",
                DEFAULT_MAX_RECONNECTS,
            )?,
            pool_cache_ttl: Duration::from_secs(parse_u64_with_default(
                "EVENT_POOL_CACHE_TTL_SECONDS",
                DEFAULT_POOL_CACHE_TTL_SECONDS,
            )?),
            min_wsol_lamports: parse_u64_with_default("EVENT_MIN_WSOL_LAMPORTS", 0)?,
            parse_retries: parse_usize_with_default(
                "EVENT_TRANSACTION_PARSE_RETRIES",
                DEFAULT_PARSE_RETRIES,
            )?,
        })
    }

    fn should_stop(&self, started: Instant, accepted_events: usize) -> bool {
        self.max_events
            .is_some_and(|target| accepted_events >= target)
            || self
                .max_seconds
                .is_some_and(|seconds| started.elapsed() >= Duration::from_secs(seconds))
    }

    fn next_wait_timeout(&self, started: Instant) -> Option<Duration> {
        let Some(max_seconds) = self.max_seconds else {
            return Some(self.update_timeout);
        };
        let remaining = Duration::from_secs(max_seconds).checked_sub(started.elapsed())?;
        Some(remaining.min(self.update_timeout))
    }
}

fn parse_optional_u64(name: &str) -> Result<Option<u64>> {
    match std::env::var(name) {
        Ok(value) => {
            let parsed = value
                .trim()
                .parse::<u64>()
                .with_context(|| format!("{name} must be an unsigned integer"))?;
            Ok((parsed > 0).then_some(parsed))
        }
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {name}")),
    }
}

fn parse_optional_usize(name: &str) -> Result<Option<usize>> {
    parse_optional_u64(name)?.map(usize::try_from).transpose().map_err(Into::into)
}

fn parse_u64_with_default(name: &str, default: u64) -> Result<u64> {
    Ok(parse_optional_u64(name)?.unwrap_or(default))
}

fn parse_usize_with_default(name: &str, default: usize) -> Result<usize> {
    Ok(parse_optional_usize(name)?.unwrap_or(default))
}

fn event_log_path() -> Result<PathBuf> {
    for name in ["EVENT_LOG_PATH", "OPPORTUNITY_LOG_PATH"] {
        match std::env::var(name) {
            Ok(value) if value.trim().is_empty() => bail!("{name} cannot be empty when configured"),
            Ok(value) => return Ok(value.into()),
            Err(std::env::VarError::NotPresent) => {}
            Err(error) => return Err(error).with_context(|| format!("failed to read {name}")),
        }
    }
    Ok("event_opportunities.jsonl".into())
}

fn monitored_programs() -> [String; 3] {
    [
        RAYDIUM_AMM_V4_PROGRAM_ID.to_owned(),
        ORCA_WHIRLPOOL_PROGRAM_ID.to_owned(),
        DLMM_PROGRAM_ID.to_owned(),
    ]
}

fn build_logs_subscribe_request(request_id: u64, program_id: &str) -> String {
    json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [program_id]},
            {"commitment": SUBSCRIPTION_COMMITMENT}
        ]
    })
    .to_string()
}

struct ProgramLogSubscriptionClient {
    socket: WebSocketStream<MaybeTlsStream<TcpStream>>,
    pending: HashMap<u64, String>,
    active: HashMap<u64, String>,
    buffered: VecDeque<RawProgramTransaction>,
}

impl ProgramLogSubscriptionClient {
    async fn connect(
        config: &HeliusConfig,
        programs: &[String],
        acknowledge_timeout: Duration,
    ) -> Result<Self> {
        if programs.is_empty() {
            bail!("event monitor requires at least one DEX program subscription");
        }
        let (socket, _) = connect_async(config.wss_url().as_str())
            .await
            .map_err(|_| anyhow::anyhow!("Helius event WSS connection failed"))?;
        let mut pending = HashMap::new();
        for (index, program) in programs.iter().enumerate() {
            pending.insert((index + 1) as u64, program.clone());
        }
        let mut client = Self {
            socket,
            pending,
            active: HashMap::new(),
            buffered: VecDeque::new(),
        };
        for (index, program) in programs.iter().enumerate() {
            let request = build_logs_subscribe_request((index + 1) as u64, program);
            client
                .socket
                .send(Message::Text(request.into()))
                .await
                .map_err(|_| anyhow::anyhow!("failed to send Helius logsSubscribe request"))?;
        }
        timeout(acknowledge_timeout, client.wait_for_acknowledgements())
            .await
            .context("timed out waiting for Helius event subscription acknowledgements")??;
        Ok(client)
    }

    async fn next_event(&mut self, wait_timeout: Duration) -> Result<RawProgramTransaction> {
        if let Some(event) = self.buffered.pop_front() {
            return Ok(event);
        }
        timeout(wait_timeout, async {
            loop {
                if let Some(event) = self.receive_message().await? {
                    return Ok(event);
                }
            }
        })
        .await
        .context("timed out waiting for DEX program transaction")?
    }

    async fn wait_for_acknowledgements(&mut self) -> Result<()> {
        while !self.pending.is_empty() {
            if let Some(event) = self.receive_message().await? {
                self.buffered.push_back(event);
            }
        }
        Ok(())
    }

    async fn receive_message(&mut self) -> Result<Option<RawProgramTransaction>> {
        let message = self
            .socket
            .next()
            .await
            .context("Helius event WSS connection closed")?
            .map_err(|_| anyhow::anyhow!("Helius event WSS receive error"))?;
        match message {
            Message::Text(text) => self.parse_text(text.as_ref()),
            Message::Close(_) => bail!("Helius event WSS connection closed"),
            Message::Ping(payload) => {
                self.socket
                    .send(Message::Pong(payload))
                    .await
                    .map_err(|_| anyhow::anyhow!("failed to reply to Helius event WSS ping"))?;
                Ok(None)
            }
            Message::Binary(_) | Message::Pong(_) | Message::Frame(_) => Ok(None),
        }
    }

    fn parse_text(&mut self, text: &str) -> Result<Option<RawProgramTransaction>> {
        let value: Value = serde_json::from_str(text).context("invalid Helius event WSS JSON")?;
        if let Some(error) = value.get("error") {
            let code = error.get("code").and_then(Value::as_i64).unwrap_or_default();
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unknown WSS error");
            bail!("Helius event WSS RPC error code={code}: {message}");
        }

        if value.get("method").and_then(Value::as_str) == Some("logsNotification") {
            let params = value
                .get("params")
                .context("logsNotification missing params")?;
            let subscription_id = params
                .get("subscription")
                .and_then(Value::as_u64)
                .context("logsNotification missing subscription id")?;
            let program_id = self
                .active
                .get(&subscription_id)
                .cloned()
                .with_context(|| {
                    format!("logsNotification for unknown subscription id {subscription_id}")
                })?;
            let result = params
                .get("result")
                .context("logsNotification missing result")?;
            let tx = result
                .get("value")
                .context("logsNotification missing value")?;
            if tx.get("err").is_some_and(|error| !error.is_null()) {
                return Ok(None);
            }
            let signature = tx
                .get("signature")
                .and_then(Value::as_str)
                .context("logsNotification missing signature")?;
            let slot = result
                .pointer("/context/slot")
                .and_then(Value::as_u64)
                .context("logsNotification missing context slot")?;
            return Ok(Some(RawProgramTransaction {
                signature: signature.to_owned(),
                slot,
                program_id,
            }));
        }

        if let Some(request_id) = value.get("id").and_then(Value::as_u64) {
            let subscription_id = value
                .get("result")
                .and_then(Value::as_u64)
                .context("logsSubscribe acknowledgement missing numeric result")?;
            let program = self
                .pending
                .remove(&request_id)
                .with_context(|| format!("unknown logsSubscribe request id {request_id}"))?;
            if self.active.insert(subscription_id, program).is_some() {
                bail!("duplicate logsSubscribe subscription id {subscription_id}");
            }
        }
        Ok(None)
    }
}

struct SignatureDeduper {
    capacity: usize,
    order: VecDeque<String>,
    seen: HashSet<String>,
}

impl SignatureDeduper {
    fn new(capacity: usize) -> Self {
        Self {
            capacity,
            order: VecDeque::with_capacity(capacity.min(4096)),
            seen: HashSet::with_capacity(capacity.min(4096)),
        }
    }

    fn insert(&mut self, signature: &str) -> bool {
        if self.seen.contains(signature) {
            return false;
        }
        let owned = signature.to_owned();
        self.seen.insert(owned.clone());
        self.order.push_back(owned);
        while self.order.len() > self.capacity {
            if let Some(oldest) = self.order.pop_front() {
                self.seen.remove(&oldest);
            }
        }
        true
    }
}

async fn fetch_enhanced_transaction(
    client: &Client,
    config: &HeliusConfig,
    signature: &str,
    retries: usize,
) -> Result<Option<Value>> {
    let retries = retries.max(1);
    for attempt in 0..retries {
        let response = client
            .post(config.enhanced_transactions_url())
            .json(&json!({"transactions": [signature]}))
            .send()
            .await
            .map_err(|_| anyhow::anyhow!("Helius transaction parse request failed"))?;
        let status = response.status();
        if !status.is_success() {
            if (status.as_u16() == 429 || status.is_server_error()) && attempt + 1 < retries {
                tokio::time::sleep(Duration::from_millis(100 * (attempt as u64 + 1))).await;
                continue;
            }
            bail!("Helius transaction parse returned status {status}");
        }
        let body = response
            .text()
            .await
            .map_err(|_| anyhow::anyhow!("failed to read Helius transaction parse response"))?;
        let parsed: Vec<Value> =
            serde_json::from_str(&body).context("invalid Helius transaction parse JSON")?;
        if let Some(transaction) = parsed
            .into_iter()
            .find(|transaction| transaction.get("signature").and_then(Value::as_str) == Some(signature))
        {
            return Ok(Some(transaction));
        }
        if attempt + 1 < retries {
            tokio::time::sleep(Duration::from_millis(100 * (attempt as u64 + 1))).await;
        }
    }
    Ok(None)
}

fn parse_raw_amount(value: Option<&Value>) -> Option<u64> {
    let value = value?;
    if let Some(number) = value.as_u64() {
        return Some(number);
    }
    value.as_str()?.parse().ok()
}

fn native_amount(swap: &Value, side: &str) -> Option<u64> {
    parse_raw_amount(swap.pointer(&format!("/{side}/amount")))
}

fn token_mints(array: Option<&Value>) -> Vec<String> {
    array
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|leg| leg.get("mint").and_then(Value::as_str))
        .map(str::to_owned)
        .collect()
}

fn raw_token_amount_for_mint(array: Option<&Value>, mint: &str) -> Option<u64> {
    let mut total = 0u64;
    let mut found = false;
    for leg in array.and_then(Value::as_array).into_iter().flatten() {
        if leg.get("mint").and_then(Value::as_str) != Some(mint) {
            continue;
        }
        let Some(amount) = parse_raw_amount(leg.pointer("/rawTokenAmount/tokenAmount")) else {
            continue;
        };
        total = total.checked_add(amount)?;
        found = true;
    }
    found.then_some(total)
}

fn parse_direct_wsol_swap(
    transaction: &Value,
    trigger: &RawProgramTransaction,
) -> Result<Option<DirectSwapEvent>> {
    if transaction.get("type").and_then(Value::as_str) != Some("SWAP") {
        return Ok(None);
    }
    let signature = transaction
        .get("signature")
        .and_then(Value::as_str)
        .context("enhanced SWAP missing signature")?;
    if signature != trigger.signature {
        bail!("enhanced SWAP signature does not match WSS trigger");
    }
    let swap = transaction
        .pointer("/events/swap")
        .context("enhanced SWAP missing events.swap")?;
    let token_inputs = swap.get("tokenInputs");
    let token_outputs = swap.get("tokenOutputs");

    let input_mints = token_mints(token_inputs);
    let output_mints = token_mints(token_outputs);
    let non_wsol = input_mints
        .iter()
        .chain(output_mints.iter())
        .filter(|mint| mint.as_str() != WSOL)
        .cloned()
        .collect::<HashSet<_>>();
    if non_wsol.len() != 1 {
        return Ok(None);
    }
    let token_mint = non_wsol.into_iter().next().expect("set length checked");
    let token_in = input_mints.iter().any(|mint| mint == &token_mint);
    let token_out = output_mints.iter().any(|mint| mint == &token_mint);
    let wsol_token_in = input_mints.iter().any(|mint| mint == WSOL);
    let wsol_token_out = output_mints.iter().any(|mint| mint == WSOL);
    let native_in = native_amount(swap, "nativeInput");
    let native_out = native_amount(swap, "nativeOutput");
    let wsol_in = native_in.is_some_and(|amount| amount > 0) || wsol_token_in;
    let wsol_out = native_out.is_some_and(|amount| amount > 0) || wsol_token_out;

    let (direction, wsol_amount_lamports, token_amount_raw) = if token_out && !token_in && wsol_in {
        (
            DirectSwapDirection::WsolToToken,
            native_in.or_else(|| raw_token_amount_for_mint(token_inputs, WSOL)),
            raw_token_amount_for_mint(token_outputs, &token_mint),
        )
    } else if token_in && !token_out && wsol_out {
        (
            DirectSwapDirection::TokenToWsol,
            native_out.or_else(|| raw_token_amount_for_mint(token_outputs, WSOL)),
            raw_token_amount_for_mint(token_inputs, &token_mint),
        )
    } else {
        return Ok(None);
    };

    Ok(Some(DirectSwapEvent {
        signature: signature.to_owned(),
        slot: trigger.slot,
        trigger_program: trigger.program_id.clone(),
        source: transaction
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("UNKNOWN")
            .to_owned(),
        direction,
        token_mint,
        wsol_amount_lamports,
        token_amount_raw,
    }))
}

fn is_supported_quote_pool(pool: &PoolInfo) -> bool {
    match pool.dex {
        Dex::Raydium => {
            pool.pool_type == "Standard"
                && pool.program_id.as_deref() == Some(RAYDIUM_AMM_V4_PROGRAM_ID)
        }
        Dex::Orca => pool.program_id.as_deref() == Some(ORCA_WHIRLPOOL_PROGRAM_ID),
        Dex::MeteoraDlmm => pool.program_id.as_deref() == Some(DLMM_PROGRAM_ID),
        Dex::MeteoraDammV2 => false,
    }
}

struct CachedPools {
    expires_at: Instant,
    pools: Vec<PoolInfo>,
}

struct DynamicPoolRegistry {
    ttl: Duration,
    by_mint: HashMap<String, CachedPools>,
}

impl DynamicPoolRegistry {
    fn new(ttl: Duration) -> Self {
        Self {
            ttl,
            by_mint: HashMap::new(),
        }
    }

    async fn pools_for_mint(&mut self, client: &Client, mint: &str) -> Result<Vec<PoolInfo>> {
        let now = Instant::now();
        if let Some(cached) = self.by_mint.get(mint) {
            if cached.expires_at > now {
                return Ok(cached.pools.clone());
            }
        }

        let discovered = discover_pair(client, mint, WSOL).await?;
        // 先过滤当前本地 quote engine 真正支持的池型，再做 per-DEX 数量限制。
        // 这样不会让未支持的高 TVL 池挤掉可报价池。
        let supported = discovered
            .into_iter()
            .filter(|pool| pool.matches_pair(mint, WSOL) && is_supported_quote_pool(pool))
            .collect::<Vec<_>>();
        let selected =
            select_monitoring_candidates(&supported, MIN_MONITOR_TVL_USD, MAX_POOLS_PER_DEX);
        self.by_mint.insert(
            mint.to_owned(),
            CachedPools {
                expires_at: now + self.ttl,
                pools: selected.clone(),
            },
        );
        Ok(selected)
    }
}

fn unix_timestamp_millis() -> Result<u64> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before Unix epoch")?
        .as_millis();
    u64::try_from(millis).context("Unix timestamp does not fit u64")
}

fn append_event_record(path: &Path, record: &EventRecord) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            create_dir_all(parent)
                .with_context(|| format!("failed to create event log directory {}", parent.display()))?;
        }
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("failed to open event log {}", path.display()))?;
    let line = serde_json::to_string(record).context("failed to serialize event record")?;
    writeln!(file, "{line}")
        .with_context(|| format!("failed to append event log {}", path.display()))?;
    Ok(())
}

pub async fn run() -> Result<()> {
    let config = HeliusConfig::from_env()?;
    let monitor_config = EventMonitorConfig::from_env()?;
    let log_path = event_log_path()?;
    let client = Client::builder().user_agent(APP_NAME).build()?;
    let programs = monitored_programs();
    let mut deduper = SignatureDeduper::new(SIGNATURE_DEDUP_CAPACITY);
    let mut registry = DynamicPoolRegistry::new(monitor_config.pool_cache_ttl);
    let started = Instant::now();
    let mut accepted_events = 0usize;
    let mut raw_program_events = 0usize;
    let mut duplicate_signatures = 0usize;
    let mut non_direct_swaps = 0usize;
    let mut below_threshold = 0usize;
    let mut reconnects = 0usize;
    let mut parse_failures = 0usize;
    let mut discovery_failures = 0usize;

    println!(
        "Event-driven V1 started: programs={} fixed_mints=0 min_wsol_lamports={} pool_cache_ttl_s={} log={}",
        programs.len(),
        monitor_config.min_wsol_lamports,
        monitor_config.pool_cache_ttl.as_secs(),
        log_path.display()
    );

    'monitor: loop {
        if monitor_config.should_stop(started, accepted_events) {
            break;
        }
        let Some(connect_timeout) = monitor_config.next_wait_timeout(started) else {
            break;
        };
        let mut subscription = match ProgramLogSubscriptionClient::connect(
            &config,
            &programs,
            connect_timeout,
        )
        .await
        {
            Ok(subscription) => subscription,
            Err(error) => {
                if reconnects >= monitor_config.max_reconnects {
                    return Err(error).context("event monitor exhausted WSS reconnect budget");
                }
                reconnects += 1;
                eprintln!("Event-driven V1 WSS reconnect #{reconnects}: {error}");
                tokio::time::sleep(Duration::from_millis(250)).await;
                continue;
            }
        };

        loop {
            if monitor_config.should_stop(started, accepted_events) {
                break 'monitor;
            }
            let Some(wait_timeout) = monitor_config.next_wait_timeout(started) else {
                break 'monitor;
            };
            let trigger = match subscription.next_event(wait_timeout).await {
                Ok(event) => event,
                Err(error) => {
                    if reconnects >= monitor_config.max_reconnects {
                        return Err(error).context("event monitor exhausted WSS reconnect budget");
                    }
                    reconnects += 1;
                    eprintln!("Event-driven V1 WSS session ended; reconnect #{reconnects}: {error}");
                    tokio::time::sleep(Duration::from_millis(250)).await;
                    continue 'monitor;
                }
            };
            raw_program_events += 1;
            if !deduper.insert(&trigger.signature) {
                duplicate_signatures += 1;
                continue;
            }

            let parsed = match fetch_enhanced_transaction(
                &client,
                &config,
                &trigger.signature,
                monitor_config.parse_retries,
            )
            .await
            {
                Ok(Some(transaction)) => transaction,
                Ok(None) => {
                    parse_failures += 1;
                    eprintln!(
                        "Event-driven V1 parse unavailable: signature={} slot={}",
                        trigger.signature, trigger.slot
                    );
                    continue;
                }
                Err(error) => {
                    parse_failures += 1;
                    eprintln!(
                        "Event-driven V1 parse failed: signature={} slot={} error={error}",
                        trigger.signature, trigger.slot
                    );
                    continue;
                }
            };
            let Some(event) = parse_direct_wsol_swap(&parsed, &trigger)? else {
                non_direct_swaps += 1;
                continue;
            };
            if monitor_config.min_wsol_lamports > 0
                && event
                    .wsol_amount_lamports
                    .is_none_or(|amount| amount < monitor_config.min_wsol_lamports)
            {
                below_threshold += 1;
                continue;
            }

            let discovery_started = Instant::now();
            let (pool_discovery_ok, pools) = match registry.pools_for_mint(&client, &event.token_mint).await {
                Ok(pools) => (true, pools),
                Err(error) => {
                    discovery_failures += 1;
                    eprintln!(
                        "Event-driven V1 pool discovery failed: mint={} signature={} error={error}",
                        event.token_mint, event.signature
                    );
                    (false, Vec::new())
                }
            };
            let pool_discovery_ms =
                u64::try_from(discovery_started.elapsed().as_millis()).unwrap_or(u64::MAX);
            let candidate_pools = pools.iter().map(EventPoolRecord::from).collect::<Vec<_>>();
            let record = EventRecord {
                schema: EVENT_SCHEMA,
                observed_at_unix_ms: unix_timestamp_millis()?,
                signature: event.signature.clone(),
                slot: event.slot,
                trigger_program: event.trigger_program.clone(),
                source: event.source.clone(),
                direction: event.direction,
                token_mint: event.token_mint.clone(),
                wsol_amount_lamports: event.wsol_amount_lamports,
                token_amount_raw: event.token_amount_raw,
                pool_discovery_ok,
                pool_discovery_ms,
                candidate_pool_count: candidate_pools.len(),
                candidate_pools,
            };
            append_event_record(&log_path, &record)?;
            accepted_events += 1;
            println!(
                "Event-driven V1 event #{accepted_events}: slot={} mint={} direction={:?} wsol_lamports={:?} pools={} discovery_ms={} signature={}",
                event.slot,
                event.token_mint,
                event.direction,
                event.wsol_amount_lamports,
                pools.len(),
                pool_discovery_ms,
                event.signature
            );
        }
    }

    println!(
        "Event-driven V1 completed: accepted_events={accepted_events} raw_program_events={raw_program_events} duplicate_signatures={duplicate_signatures} non_direct_swaps={non_direct_swaps} below_threshold={below_threshold} parse_failures={parse_failures} discovery_failures={discovery_failures} reconnects={reconnects} fixed_mints=0"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn trigger(signature: &str) -> RawProgramTransaction {
        RawProgramTransaction {
            signature: signature.into(),
            slot: 123,
            program_id: RAYDIUM_AMM_V4_PROGRAM_ID.into(),
        }
    }

    #[test]
    fn parses_wsol_to_dynamic_token_swap() {
        let transaction = json!({
            "type": "SWAP",
            "source": "RAYDIUM",
            "signature": "sig-a",
            "events": {
                "swap": {
                    "nativeInput": {"account": "wallet", "amount": "1000000000"},
                    "tokenInputs": [],
                    "tokenOutputs": [{
                        "mint": "DynamicMint111111111111111111111111111111111",
                        "rawTokenAmount": {"tokenAmount": "2500000", "decimals": 6}
                    }]
                }
            }
        });
        let event = parse_direct_wsol_swap(&transaction, &trigger("sig-a"))
            .unwrap()
            .unwrap();
        assert_eq!(event.token_mint, "DynamicMint111111111111111111111111111111111");
        assert_eq!(event.direction, DirectSwapDirection::WsolToToken);
        assert_eq!(event.wsol_amount_lamports, Some(1_000_000_000));
        assert_eq!(event.token_amount_raw, Some(2_500_000));
    }

    #[test]
    fn parses_dynamic_token_to_wsol_swap() {
        let transaction = json!({
            "type": "SWAP",
            "source": "ORCA",
            "signature": "sig-b",
            "events": {
                "swap": {
                    "nativeOutput": {"account": "wallet", "amount": "500000000"},
                    "tokenInputs": [{
                        "mint": "AnotherMint11111111111111111111111111111111",
                        "rawTokenAmount": {"tokenAmount": "9000000", "decimals": 6}
                    }],
                    "tokenOutputs": []
                }
            }
        });
        let event = parse_direct_wsol_swap(&transaction, &trigger("sig-b"))
            .unwrap()
            .unwrap();
        assert_eq!(event.direction, DirectSwapDirection::TokenToWsol);
        assert_eq!(event.wsol_amount_lamports, Some(500_000_000));
        assert_eq!(event.token_amount_raw, Some(9_000_000));
    }

    #[test]
    fn rejects_multihop_swap_with_multiple_dynamic_mints() {
        let transaction = json!({
            "type": "SWAP",
            "source": "JUPITER",
            "signature": "sig-c",
            "events": {
                "swap": {
                    "nativeInput": {"account": "wallet", "amount": "100000000"},
                    "tokenInputs": [],
                    "tokenOutputs": [
                        {"mint": "MintA", "rawTokenAmount": {"tokenAmount": "1", "decimals": 6}},
                        {"mint": "MintB", "rawTokenAmount": {"tokenAmount": "1", "decimals": 6}}
                    ]
                }
            }
        });
        assert!(parse_direct_wsol_swap(&transaction, &trigger("sig-c"))
            .unwrap()
            .is_none());
    }

    #[test]
    fn ignores_non_swap_transaction() {
        let transaction = json!({"type": "TRANSFER", "signature": "sig-d"});
        assert!(parse_direct_wsol_swap(&transaction, &trigger("sig-d"))
            .unwrap()
            .is_none());
    }

    #[test]
    fn signature_deduper_is_bounded_and_allows_evicted_signature_again() {
        let mut deduper = SignatureDeduper::new(2);
        assert!(deduper.insert("a"));
        assert!(!deduper.insert("a"));
        assert!(deduper.insert("b"));
        assert!(deduper.insert("c"));
        assert!(deduper.insert("a"));
        assert!(deduper.order.len() <= 2);
        assert!(deduper.seen.len() <= 2);
    }

    #[test]
    fn dynamic_pool_filter_only_keeps_locally_quoteable_types() {
        let raydium_supported = PoolInfo {
            dex: Dex::Raydium,
            address: "ray".into(),
            pool_type: "Standard".into(),
            program_id: Some(RAYDIUM_AMM_V4_PROGRAM_ID.into()),
            mint_a: "M".into(),
            mint_b: WSOL.into(),
            tvl_usd: 10_000.0,
        };
        let raydium_unsupported = PoolInfo {
            dex: Dex::Raydium,
            address: "clmm".into(),
            pool_type: "Concentrated".into(),
            program_id: Some("unsupported".into()),
            mint_a: "M".into(),
            mint_b: WSOL.into(),
            tvl_usd: 1_000_000.0,
        };
        assert!(is_supported_quote_pool(&raydium_supported));
        assert!(!is_supported_quote_pool(&raydium_unsupported));
    }
}
