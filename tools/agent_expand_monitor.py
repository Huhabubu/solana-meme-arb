from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def sub_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one regex match, found {count}")
    return updated


app_path = Path("src/app.rs")
app = app_path.read_text()

app = replace_once(
    app,
    "use std::{\n    collections::{HashMap, HashSet},\n    str::FromStr,\n    time::{Duration, Instant, SystemTime, UNIX_EPOCH},\n};",
    "use std::{\n    collections::{HashMap, HashSet},\n    fs,\n    path::Path,\n    str::FromStr,\n    time::{Duration, Instant, SystemTime, UNIX_EPOCH},\n};",
    "std imports",
)

app = replace_once(
    app,
    "    rpc::{\n        fetch_account_owners, fetch_accounts, is_min_context_slot_not_reached,\n        verify_pool_accounts, PUBLIC_MAINNET_RPC,\n    },",
    "    rpc::{\n        fetch_account_owners, fetch_accounts, fetch_accounts_coherent,\n        is_min_context_slot_not_reached, verify_pool_accounts, PUBLIC_MAINNET_RPC,\n    },",
    "rpc imports",
)

app = sub_once(
    app,
    r"const FIXED_V3_POOL_ADDRESSES: \[&str; 6\] = \[.*?\];\n",
    "",
    "remove fixed pool addresses",
)

selection_block = '''async fn discover_supported_quote_pools(client: &Client, token: &Token) -> Result<Vec<PoolInfo>> {
    let discovered = discover_quote_pair(client, token.mint, WSOL).await?;
    let pools = supported_quote_pools(&discovered);
    if pools.len() < 2 {
        bail!(
            "{} / WSOL: arbitrage monitoring requires at least 2 supported pools, found {}",
            token.symbol,
            pools.len()
        );
    }
    Ok(pools)
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

fn supported_quote_pools(discovered: &[PoolInfo]) -> Vec<PoolInfo> {
    let supported = discovered
        .iter()
        .filter(|pool| is_supported_quote_pool(pool))
        .cloned()
        .collect::<Vec<_>>();
    select_monitoring_candidates(&supported, MIN_MONITOR_TVL_USD, MAX_POOLS_PER_DEX)
}
'''
app = sub_once(
    app,
    r"async fn discover_supported_quote_pools\(.*?\nfn token_symbol_for_pool",
    selection_block + "\nfn token_symbol_for_pool",
    "supported pool selection",
)

app = replace_once(
    app,
    "    let mut verified_routes = 0usize;\n",
    "    let mut verified_routes = 0usize;\n    let mut expected_routes = 0usize;\n",
    "round-trip expected routes counter",
)

app = replace_once(
    app,
    '''        if pools.len() != 3 {
            bail!(
                "{}/WSOL expected exactly 3 V3 quoteable pools, got {}",
                token.symbol,
                pools.len()
            );
        }
        let routes = directed_route_indices(pools.len());
        if routes.len() != 6 {
            bail!(
                "{}/WSOL expected 6 directed two-pool routes, got {}",
                token.symbol,
                routes.len()
            );
        }
''',
    '''        let routes = directed_route_indices(pools.len());
        if routes.is_empty() {
            bail!("{}/WSOL produced no directed two-pool routes", token.symbol);
        }
        expected_routes += routes.len();
''',
    "remove fixed three-pool round-trip assumption",
)

app = replace_once(
    app,
    "    let expected_routes = tracked_tokens().len() * 6;\n    let expected_points = expected_routes * ROUND_TRIP_PROBE_LAMPORTS.len();",
    "    let expected_points = expected_routes * ROUND_TRIP_PROBE_LAMPORTS.len();",
    "dynamic round-trip expected totals",
)

app = replace_once(
    app,
    '        bail!("cannot build QuoteState for an empty fixed pool universe");',
    '        bail!("cannot build QuoteState for an empty selected pool universe");',
    "quote state empty universe wording",
)
app = replace_once(
    app,
    '                "duplicate pool in fixed QuoteState universe: {}",',
    '                "duplicate pool in selected QuoteState universe: {}",',
    "quote state duplicate wording",
)

app = replace_once(
    app,
    '''    if pools.is_empty() {
        bail!("V2 quoteable universe is empty");
    }
    let state = build_quote_state_for_pools(client, config, &pools).await?;
''',
    '''    if pools.is_empty() {
        bail!("V2 quoteable universe is empty");
    }
    println!("V3 selected research universe: {} pools", pools.len());
    for pool in &pools {
        println!(
            "  dex={} pool={} tvl_usd={:.2} type={} pair={}/{}",
            pool.dex, pool.address, pool.tvl_usd, pool.pool_type, pool.mint_a, pool.mint_b
        );
    }
    let state = build_quote_state_for_pools(client, config, &pools).await?;
''',
    "research universe logging",
)

app = replace_once(
    app,
    '''    let batch = fetch_accounts(
        client,
        config.http_url().as_str(),
        &addresses,
        min_context_slot,
    )
    .await?;
    if batch.accounts.len() != addresses.len() {
        bail!("dependency snapshot account count mismatch");
    }
''',
    '''    let batch = fetch_accounts_coherent(
        client,
        config.http_url().as_str(),
        &addresses,
        min_context_slot,
    )
    .await?;
    if batch.accounts.len() != addresses.len() {
        bail!("dependency snapshot account count mismatch");
    }
''',
    "chunked coherent preload",
)

app = replace_once(
    app,
    '''        let batch = fetch_accounts(
            client,
            config.http_url().as_str(),
            &request_addresses,
            Some(min_context_slot),
        )
        .await?;
''',
    '''        let batch = fetch_accounts_coherent(
            client,
            config.http_url().as_str(),
            &request_addresses,
            Some(min_context_slot),
        )
        .await?;
''',
    "chunked coherent route snapshot",
)

manifest_helpers = '''fn pool_universe_manifest_contents(pools: &[PoolInfo]) -> String {
    let mut entries = pools
        .iter()
        .map(|pool| {
            format!(
                "{}\\t{}\\t{}\\t{}\\t{}",
                pool.address, pool.dex, pool.pool_type, pool.mint_a, pool.mint_b
            )
        })
        .collect::<Vec<_>>();
    entries.sort();
    entries.push(String::new());
    entries.join("\\n")
}

fn ensure_pool_universe_manifest(log_path: &Path, pools: &[PoolInfo]) -> Result<()> {
    let manifest_path = log_path.with_extension("universe");
    let expected = pool_universe_manifest_contents(pools);
    if manifest_path.exists() {
        let stored = fs::read_to_string(&manifest_path).with_context(|| {
            format!(
                "failed to read pool universe manifest: {}",
                manifest_path.display()
            )
        })?;
        if stored != expected {
            bail!(
                "selected pool universe differs from {}; use a new OPPORTUNITY_LOG_PATH for this research sample",
                manifest_path.display()
            );
        }
        return Ok(());
    }

    if log_path.exists()
        && fs::metadata(log_path)
            .with_context(|| format!("failed to inspect opportunity log: {}", log_path.display()))?
            .len()
            > 0
    {
        bail!(
            "existing opportunity log has no pool universe manifest; use a new OPPORTUNITY_LOG_PATH before changing the monitored pool set"
        );
    }
    if let Some(parent) = manifest_path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent).with_context(|| {
            format!(
                "failed to create pool universe manifest directory: {}",
                parent.display()
            )
        })?;
    }
    fs::write(&manifest_path, expected).with_context(|| {
        format!(
            "failed to write pool universe manifest: {}",
            manifest_path.display()
        )
    })?;
    Ok(())
}

'''
app = replace_once(
    app,
    "struct CoherentRouteSnapshot {\n",
    manifest_helpers + "struct CoherentRouteSnapshot {\n",
    "pool universe manifest helpers",
)

new_observation_block = '''    let route_pools = unique_route_pools(&routes);
    let snapshot_started = Instant::now();
    let initial_snapshot =
        refresh_coherent_route_snapshot(client, config, state, cache, &route_pools, update.slot)
            .await?;
    let mut snapshot_duration = snapshot_started.elapsed();
    let quote_started = Instant::now();
    let initial_events = evaluate_coherent_routes(cache, &routes, &initial_snapshot)?;
    let mut quote_duration = quote_started.elapsed();
    let initial_positive = contains_net_positive(&initial_events);
    let mut observations = vec![(
        initial_snapshot.slot,
        unix_timestamp_millis()?,
        initial_snapshot.meteora_clock.is_some(),
        initial_events,
    )];
    let mut confirmation_wait_duration = Duration::ZERO;
    if initial_positive {
        let confirmation_slot = initial_snapshot.slot.saturating_add(1);
        println!(
            "V3.6 net-positive candidate at coherent slot={}; recording initial observation and confirming at min_slot={confirmation_slot}",
            initial_snapshot.slot
        );
        let confirmation_wait_started = Instant::now();
        tokio::time::sleep(Duration::from_millis(POSITIVE_CONFIRMATION_DELAY_MILLIS)).await;
        confirmation_wait_duration = confirmation_wait_started.elapsed();
        let confirmation_snapshot_started = Instant::now();
        let confirmation_snapshot = refresh_coherent_route_snapshot(
            client,
            config,
            state,
            cache,
            &route_pools,
            confirmation_slot,
        )
        .await?;
        snapshot_duration += confirmation_snapshot_started.elapsed();
        let confirmation_quote_started = Instant::now();
        let confirmation_events = evaluate_coherent_routes(cache, &routes, &confirmation_snapshot)?;
        quote_duration += confirmation_quote_started.elapsed();
        observations.push((
            confirmation_snapshot.slot,
            unix_timestamp_millis()?,
            confirmation_snapshot.meteora_clock.is_some(),
            confirmation_events,
        ));
    }

    let observation_count = observations.len();
    let mut evaluated_count = 0usize;
    let mut unavailable_count = 0usize;
    let mut net_positive_count = 0usize;
    let mut records = Vec::with_capacity(
        routes.len() * ROUND_TRIP_PROBE_LAMPORTS.len() * observation_count,
    );

    for (observation_index, (snapshot_slot, observed_at_unix_ms, clock_in_snapshot, events)) in
        observations.into_iter().enumerate()
    {
        let observation_kind = if observation_index == 0 {
            "initial"
        } else {
            "confirmation"
        };
        println!(
            "V3.6 coherent recompute: observation={observation_kind} slot={snapshot_slot} {} affected pool(s), {} related route(s), clock_in_snapshot={clock_in_snapshot}",
            affected_pools.len(),
            routes.len(),
        );
        for event in &events {
            let token = tracked_tokens()
                .iter()
                .find(|token| token.mint == event.token_mint)
                .context("route token is outside tracked universe")?;
            match &event.outcome {
                OpportunityEventOutcome::Evaluated {
                    gross_profit_raw,
                    net_profit_raw,
                    ..
                } => {
                    evaluated_count += 1;
                    if *net_profit_raw > 0 {
                        net_positive_count += 1;
                    }
                    println!(
                        "{}/WSOL monitor event: observation={observation_kind} {}->{} input={} gross_profit_raw={} net_profit_raw={}",
                        token.symbol,
                        event.first_dex,
                        event.second_dex,
                        event.input_amount,
                        gross_profit_raw,
                        net_profit_raw
                    );
                }
                OpportunityEventOutcome::InsufficientLiquidity { stage } => {
                    unavailable_count += 1;
                    println!(
                        "{}/WSOL monitor event: observation={observation_kind} {}->{} input={} status=insufficient_liquidity stage={stage:?}",
                        token.symbol,
                        event.first_dex,
                        event.second_dex,
                        event.input_amount
                    );
                }
            }
            records.push(OpportunityRecord::from_event(
                event,
                observed_at_unix_ms,
                update.slot,
                &update.address,
                update.subscription_id,
            )?);
        }
    }

    let expected_records =
        routes.len() * ROUND_TRIP_PROBE_LAMPORTS.len() * observation_count;
    if records.len() != expected_records || evaluated_count + unavailable_count != records.len() {
        bail!("opportunity update event accounting mismatch");
    }
'''
app = sub_once(
    app,
    r"    let route_pools = unique_route_pools\(&routes\);.*?    if records\.len\(\) != expected_records \|\| evaluated_count \+ unavailable_count != records\.len\(\) \{\n        bail!\(\"opportunity update event accounting mismatch\"\);\n    \}\n",
    new_observation_block,
    "preserve initial and confirmation observations",
)

app = replace_once(
    app,
    '''    let mut cumulative_stats = if log_path.exists() {
        scan_records(&log_path)?
    } else {
        Default::default()
    };
    let initial_record_count = cumulative_stats.total;
    let mut log_writer = OpportunityLogWriter::open(&log_path)?;
    let (initial_state, pools) = build_quote_state(client, &config).await?;
    let mut state = initial_state;
''',
    '''    let (initial_state, pools) = build_quote_state(client, &config).await?;
    ensure_pool_universe_manifest(&log_path, &pools)?;
    let mut cumulative_stats = if log_path.exists() {
        scan_records(&log_path)?
    } else {
        Default::default()
    };
    let initial_record_count = cumulative_stats.total;
    let mut log_writer = OpportunityLogWriter::open(&log_path)?;
    let mut state = initial_state;
''',
    "monitor universe manifest startup",
)

selection_test = '''    #[test]
    fn supported_pool_selection_filters_before_per_dex_top_n_and_keeps_multiple_pools() {
        let mut candidates = vec![
            pool(Dex::Raydium, "Concentrated", "clmm", "ray-clmm"),
            pool(Dex::Raydium, "Standard", RAYDIUM_AMM_V4_PROGRAM_ID, "ray-1"),
            pool(Dex::Raydium, "Standard", RAYDIUM_AMM_V4_PROGRAM_ID, "ray-2"),
            pool(Dex::Raydium, "Standard", RAYDIUM_AMM_V4_PROGRAM_ID, "ray-3"),
            pool(Dex::Raydium, "Standard", RAYDIUM_AMM_V4_PROGRAM_ID, "ray-4"),
            pool(Dex::Orca, "whirlpool", ORCA_WHIRLPOOL_PROGRAM_ID, "orca-1"),
            pool(Dex::Orca, "whirlpool", ORCA_WHIRLPOOL_PROGRAM_ID, "orca-2"),
            pool(Dex::MeteoraDlmm, "DLMM", DLMM_PROGRAM_ID, "meteora-1"),
            pool(Dex::MeteoraDlmm, "DLMM", DLMM_PROGRAM_ID, "meteora-2"),
            pool(Dex::MeteoraDammV2, "DAMM v2", "damm", "damm"),
        ];
        for (index, candidate) in candidates.iter_mut().enumerate() {
            candidate.tvl_usd = 20_000.0 - index as f64 * 1_000.0;
        }
        candidates[0].tvl_usd = 100_000.0;

        let selected = supported_quote_pools(&candidates);
        let addresses = selected
            .iter()
            .map(|pool| pool.address.as_str())
            .collect::<HashSet<_>>();
        assert!(selected.iter().all(is_supported_quote_pool));
        assert_eq!(
            selected.iter().filter(|pool| pool.dex == Dex::Raydium).count(),
            MAX_POOLS_PER_DEX
        );
        assert!(addresses.contains("ray-1"));
        assert!(addresses.contains("ray-2"));
        assert!(addresses.contains("ray-3"));
        assert!(!addresses.contains("ray-4"));
        assert!(!addresses.contains("ray-clmm"));
        assert!(addresses.contains("orca-1"));
        assert!(addresses.contains("orca-2"));
        assert!(addresses.contains("meteora-1"));
        assert!(addresses.contains("meteora-2"));
    }

    #[test]
    fn pool_universe_manifest_is_order_independent() {
        let first = pool(Dex::Raydium, "Standard", RAYDIUM_AMM_V4_PROGRAM_ID, "ray");
        let second = pool(Dex::Orca, "whirlpool", ORCA_WHIRLPOOL_PROGRAM_ID, "orca");
        assert_eq!(
            pool_universe_manifest_contents(&[first.clone(), second.clone()]),
            pool_universe_manifest_contents(&[second, first])
        );
    }

'''
app = sub_once(
    app,
    r"    #\[test\]\n    fn supported_pool_selection_keeps_only_current_quote_engines\(\) \{.*?\n    #\[test\]\n    fn quote_probe_sizes_are_stable",
    selection_test + "    #[test]\n    fn quote_probe_sizes_are_stable",
    "selection tests",
)

if "FIXED_V3_POOL_ADDRESSES" in app or "fixed V3 universe" in app:
    raise RuntimeError("fixed-pool assumptions remain in src/app.rs")
app_path.write_text(app)

rpc_path = Path("src/rpc.rs")
rpc = rpc_path.read_text()
rpc = replace_once(
    rpc,
    "use base64::{engine::general_purpose::STANDARD as BASE64, Engine};\nuse reqwest::{header::RETRY_AFTER, Client, StatusCode};",
    "use base64::{engine::general_purpose::STANDARD as BASE64, Engine};\nuse futures_util::future::try_join_all;\nuse reqwest::{header::RETRY_AFTER, Client, StatusCode};",
    "rpc try_join_all import",
)
rpc = replace_once(
    rpc,
    "const TRANSIENT_HTTP_RETRY_MAX_MS: u64 = 30_000;",
    "const TRANSIENT_HTTP_RETRY_MAX_MS: u64 = 30_000;\nconst GET_MULTIPLE_ACCOUNTS_MAX_ADDRESSES: usize = 100;\nconst COHERENT_ACCOUNT_BATCH_MAX_ATTEMPTS: usize = 4;",
    "rpc coherent batch constants",
)
rpc = replace_once(
    rpc,
    '    if addresses.len() > 100 {\n        bail!("getMultipleAccounts supports at most 100 addresses per request");\n    }',
    '    if addresses.len() > GET_MULTIPLE_ACCOUNTS_MAX_ADDRESSES {\n        bail!("getMultipleAccounts supports at most 100 addresses per request");\n    }',
    "owner request limit constant",
)
rpc = replace_once(
    rpc,
    '    if addresses.len() > 100 {\n        bail!("getMultipleAccounts supports at most 100 addresses per request");\n    }',
    '    if addresses.len() > GET_MULTIPLE_ACCOUNTS_MAX_ADDRESSES {\n        bail!("getMultipleAccounts supports at most 100 addresses per request");\n    }',
    "full request limit constant",
)

coherent_fetch = '''
/// 读取任意数量账户，并要求所有分片最终来自同一个 RPC context slot。
///
/// Solana `getMultipleAccounts` 单次最多 100 个地址。研究监控扩池后会超过该限制，
/// 因此这里并发请求多个分片；若各分片落在不同 context slot，就把最高 slot 作为
/// 下一轮 `minContextSlot` 并整体重试。只有所有分片 slot 完全一致时才返回。
pub async fn fetch_accounts_coherent(
    client: &Client,
    rpc_url: &str,
    addresses: &[String],
    min_context_slot: Option<u64>,
) -> Result<AccountBatch> {
    if addresses.len() <= GET_MULTIPLE_ACCOUNTS_MAX_ADDRESSES {
        return fetch_accounts(client, rpc_url, addresses, min_context_slot).await;
    }

    let mut required_slot = min_context_slot;
    for attempt in 1..=COHERENT_ACCOUNT_BATCH_MAX_ATTEMPTS {
        let batches = try_join_all(addresses.chunks(GET_MULTIPLE_ACCOUNTS_MAX_ADDRESSES).map(
            |chunk| fetch_accounts(client, rpc_url, chunk, required_slot),
        ))
        .await?;
        let target_slot = batches
            .iter()
            .map(|batch| batch.slot)
            .max()
            .context("coherent account batch unexpectedly produced no chunks")?;
        if batches.iter().all(|batch| batch.slot == target_slot) {
            let accounts = batches
                .into_iter()
                .flat_map(|batch| batch.accounts)
                .collect::<Vec<_>>();
            if accounts.len() != addresses.len() {
                bail!("coherent account batch result length mismatch");
            }
            return Ok(AccountBatch {
                slot: target_slot,
                accounts,
            });
        }
        if attempt < COHERENT_ACCOUNT_BATCH_MAX_ATTEMPTS {
            required_slot = Some(target_slot);
        }
    }

    bail!(
        "could not obtain one coherent RPC context slot across {} account chunks",
        addresses.len().div_ceil(GET_MULTIPLE_ACCOUNTS_MAX_ADDRESSES)
    )
}
'''
rpc = replace_once(
    rpc,
    "\nfn build_full_accounts_request(\n",
    coherent_fetch + "\nfn build_full_accounts_request(\n",
    "coherent multi-chunk fetch helper",
)
rpc_path.write_text(rpc)

readme_path = Path("README.md")
readme = readme_path.read_text()
readme = readme.replace(
    "| 三 DEX 全有向路径 | ✅ | 每 Token 3 池 → 6 路；BONK/WIF 共 12 路 |",
    "| 支持池全有向路径 | ✅ | 先过滤当前 Quote Engine 支持池型，再按每 DEX TVL Top-N，生成全部有向两池路径 |",
)
readme = readme.replace(
    "| affected-route 实时路由 | ✅ | WSS 仅作触发；相关两腿依赖通过同一次 `getMultipleAccounts` 刷新 |",
    "| affected-route 实时路由 | ✅ | WSS 仅作触发；相关路径依赖刷新到同一 RPC context slot，>100 账户时自动分片并一致性重试 |",
)
readme = re.sub(
    r"当前完整支持 6 个研究池：\n\n\| Token \| DEX \| Pool \|\n\|---\|---\|---\|\n(?:\|.*\n){6}",
    "当前 Token Universe 仍为 BONK/WIF；Pool Universe 不再写死地址。启动时会先过滤当前 Quote Engine 支持的池型，再按每个 DEX 的 TVL 选择最多 `MAX_POOLS_PER_DEX` 个池。程序会把本次选择写入与 JSONL 同名的 `.universe` 清单；重启后若池集合变化，会拒绝继续写旧样本，要求新建 `OPPORTUNITY_LOG_PATH`。\n\n",
    readme,
    count=1,
)
readme = readme.replace(
    "一次 getMultipleAccounts 刷新两腿全部依赖与 Clock",
    "coherent getMultipleAccounts 刷新相关路径全部依赖与 Clock（>100 时分片并对齐同一 context slot）",
)
readme = readme.replace(
    "若 net-positive，等待并用下一 slot 的一致快照复核\n        ↓\nOpportunityRecord",
    "首次一致快照先写入 OpportunityRecord\n        ↓\n若 net-positive，等待并用下一 slot 的一致快照复核，再追加第二组 OpportunityRecord",
)
readme = readme.replace(
    "修复后必须重新采样；只有通过一致快照与下一 slot 复核的正值才进入后续分析。",
    "修复后必须重新采样；首次一致快照与下一 slot 复核会同时保留，后续离线分析据此判断机会持续时间和可执行性。",
)
readme = readme.replace(
    "- 固定 BONK/WIF 的 6 个 V3 研究池，进程重启不再静默切换 Universe。",
    "- Pool Universe 改为支持池型优先过滤 + 每 DEX TVL Top-N；每个样本文件绑定 `.universe` 清单，防止重启后静默混入不同池集合。",
)
readme_path.write_text(readme)

sampling_path = Path("docs/V3_SAMPLING_LOG.md")
sampling = sampling_path.read_text()
sampling = sampling.replace(
    "因此旧样本中的正值不能解释为可执行套利。修复后的 monitor 把 WSS 仅作为触发信号，通过一次 `getMultipleAccounts` 为相关两腿建立同一 RPC snapshot；若首次结果为正，再跨到下一 slot 复核。旧 artifact 保留为缺陷证据，不与修复后的样本合并统计。",
    "因此旧样本中的正值不能解释为可执行套利。修复后的 monitor 把 WSS 仅作为触发信号，为相关路径建立同一 RPC context slot 的一致快照；超过 100 个账户时自动分片并重试到同一 context slot。若首次结果为正，首次观察先原样落盘，再跨到下一 slot 复核并追加第二次观察。旧 artifact 保留为缺陷证据，不与修复后的样本合并统计。",
)
sampling = sampling.replace(
    "同时完成：固定 6 个 V3 研究池；JSONL 改为流式重放并仅恢复未写完的最后一行；配置模板统一为 `HELIUS_API_KEY`。",
    "同时完成：Pool Universe 改为支持池型过滤后按每 DEX TVL Top-N 选择，并用 `.universe` 清单绑定每份样本；JSONL 改为流式重放并仅恢复未写完的最后一行；配置模板统一为 `HELIUS_API_KEY`。",
)
sampling_path.write_text(sampling)

print("agent monitor expansion patch applied")
