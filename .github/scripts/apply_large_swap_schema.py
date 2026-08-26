from pathlib import Path

SRC = Path("src/event_monitor_v2.rs")
SMOKE = Path(".github/workflows/event-live-smoke.yml")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"patch anchor missing: {label}")
    return text.replace(old, new, 1)


def patch_source() -> bool:
    s = SRC.read_text()
    if 'const EVENT_SCHEMA: &str = "large-swap-event-v1";' in s:
        print("source schema already upgraded")
        return False

    s = replace_once(
        s,
        'const EVENT_SCHEMA: &str = "event-driven-v2";',
        'const EVENT_SCHEMA: &str = "large-swap-event-v1";',
        "schema",
    )

    s = replace_once(
        s,
        "    second_dex: String,\n    second_pool: String,\n    input_lamports: u64,",
        "    second_dex: String,\n    second_pool: String,\n    route_contains_trigger_pool: Option<bool>,\n    input_lamports: u64,",
        "opportunity field",
    )

    constructor = (
        "                second_dex: event.second_dex.to_string(),\n"
        "                second_pool: event.second_pool.clone(),\n"
        "                input_lamports: event.input_amount,"
    )
    constructor_new = (
        "                second_dex: event.second_dex.to_string(),\n"
        "                second_pool: event.second_pool.clone(),\n"
        "                route_contains_trigger_pool: None,\n"
        "                input_lamports: event.input_amount,"
    )
    if s.count(constructor) != 2:
        raise SystemExit(f"expected two OpportunityRecord constructors, found {s.count(constructor)}")
    s = s.replace(constructor, constructor_new)

    s = replace_once(
        s,
        "    event_slot: u64,\n    trigger_program: String,\n    source: String,",
        "    event_slot: u64,\n"
        "    trigger_program: String,\n"
        "    trigger_dex: Option<&'static str>,\n"
        "    trigger_pool: Option<String>,\n"
        "    trigger_pool_match_count: usize,\n"
        "    source: String,",
        "event trigger fields",
    )

    s = replace_once(
        s,
        "    token_amount_raw: Option<u64>,\n    pool_discovery_ok: bool,",
        "    token_amount_raw: Option<u64>,\n"
        "    trigger_received_at_unix_ms: u64,\n"
        "    tx_parsed_at_unix_ms: u64,\n"
        "    pool_discovery_done_at_unix_ms: u64,\n"
        "    quote_done_at_unix_ms: u64,\n"
        "    parse_ms: u64,\n"
        "    total_event_to_quote_ms: u64,\n"
        "    pool_discovery_ok: bool,",
        "event timing fields",
    )

    helpers = r'''
fn transaction_account_keys(transaction: &Value) -> HashSet<String> {
    transaction
        .get("accountData")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.get("account").and_then(Value::as_str))
        .map(str::to_owned)
        .collect()
}

fn trigger_dex_for_program(program_id: &str) -> Option<&'static str> {
    match program_id {
        RAYDIUM_AMM_V4_PROGRAM_ID => Some("raydium"),
        ORCA_WHIRLPOOL_PROGRAM_ID => Some("orca"),
        DLMM_PROGRAM_ID => Some("meteora_dlmm"),
        _ => None,
    }
}

fn resolve_trigger_pool(transaction: &Value, pools: &[PoolInfo]) -> (Option<String>, usize) {
    let accounts = transaction_account_keys(transaction);
    let matches = pools
        .iter()
        .filter(|pool| accounts.contains(&pool.address))
        .collect::<Vec<_>>();
    let count = matches.len();
    let resolved = (count == 1).then(|| matches[0].address.clone());
    (resolved, count)
}

'''
    s = replace_once(
        s,
        "fn is_supported_quote_pool(pool: &PoolInfo) -> bool {",
        helpers + "fn is_supported_quote_pool(pool: &PoolInfo) -> bool {",
        "trigger-pool helpers",
    )

    s = replace_once(
        s,
        "fn summarize_opportunities(\n    events: &[OpportunityEvent],\n) -> (",
        "fn summarize_opportunities(\n    events: &[OpportunityEvent],\n    trigger_pool: Option<&str>,\n) -> (",
        "summary signature",
    )

    s = replace_once(
        s,
        "        events.iter().map(OpportunityRecord::from).collect(),\n    )\n}",
        "        events\n"
        "            .iter()\n"
        "            .map(|event| {\n"
        "                let mut record = OpportunityRecord::from(event);\n"
        "                record.route_contains_trigger_pool = trigger_pool.map(|pool| {\n"
        "                    event.first_pool.as_str() == pool || event.second_pool.as_str() == pool\n"
        "                });\n"
        "                record\n"
        "            })\n"
        "            .collect(),\n"
        "    )\n"
        "}",
        "summary records",
    )

    s = replace_once(
        s,
        "            if !deduper.insert(&trigger.signature) {\n"
        "                duplicate_signatures += 1;\n"
        "                continue;\n"
        "            }\n\n"
        "            let parsed = match fetch_enhanced_transaction(",
        "            if !deduper.insert(&trigger.signature) {\n"
        "                duplicate_signatures += 1;\n"
        "                continue;\n"
        "            }\n\n"
        "            let event_started = Instant::now();\n"
        "            let trigger_received_at_unix_ms = unix_timestamp_millis()?;\n"
        "            let parse_started = Instant::now();\n"
        "            let parsed = match fetch_enhanced_transaction(",
        "event timing start",
    )

    s = replace_once(
        s,
        "            let Some(event) = parse_direct_wsol_swap(&parsed, &trigger)? else {\n"
        "                non_direct_swaps += 1;\n"
        "                continue;\n"
        "            };\n"
        "            if monitor_config.min_wsol_lamports > 0 {",
        "            let Some(event) = parse_direct_wsol_swap(&parsed, &trigger)? else {\n"
        "                non_direct_swaps += 1;\n"
        "                continue;\n"
        "            };\n"
        "            let parse_ms =\n"
        "                u64::try_from(parse_started.elapsed().as_millis()).unwrap_or(u64::MAX);\n"
        "            let tx_parsed_at_unix_ms = unix_timestamp_millis()?;\n"
        "            if monitor_config.min_wsol_lamports > 0 {",
        "parse timing finish",
    )

    s = replace_once(
        s,
        "            let pool_discovery_ms =\n"
        "                u64::try_from(discovery_started.elapsed().as_millis()).unwrap_or(u64::MAX);\n\n"
        "            let quote_started = Instant::now();",
        "            let pool_discovery_ms =\n"
        "                u64::try_from(discovery_started.elapsed().as_millis()).unwrap_or(u64::MAX);\n"
        "            let pool_discovery_done_at_unix_ms = unix_timestamp_millis()?;\n"
        "            let trigger_dex = trigger_dex_for_program(&event.trigger_program);\n"
        "            let (trigger_pool, trigger_pool_match_count) = resolve_trigger_pool(&parsed, &pools);\n\n"
        "            let quote_started = Instant::now();",
        "pool resolution",
    )

    s = replace_once(
        s,
        "            let quote_eval_ms =\n"
        "                u64::try_from(quote_started.elapsed().as_millis()).unwrap_or(u64::MAX);\n"
        "            let (evaluated_count, net_positive_count, best_profit, best_return, opportunities) =\n"
        "                summarize_opportunities(&opportunity_events);",
        "            let quote_eval_ms =\n"
        "                u64::try_from(quote_started.elapsed().as_millis()).unwrap_or(u64::MAX);\n"
        "            let quote_done_at_unix_ms = unix_timestamp_millis()?;\n"
        "            let total_event_to_quote_ms =\n"
        "                u64::try_from(event_started.elapsed().as_millis()).unwrap_or(u64::MAX);\n"
        "            let (evaluated_count, net_positive_count, best_profit, best_return, opportunities) =\n"
        "                summarize_opportunities(&opportunity_events, trigger_pool.as_deref());",
        "quote timing finish",
    )

    s = replace_once(
        s,
        "                event_slot: event.slot,\n"
        "                trigger_program: event.trigger_program.clone(),\n"
        "                source: event.source.clone(),",
        "                event_slot: event.slot,\n"
        "                trigger_program: event.trigger_program.clone(),\n"
        "                trigger_dex,\n"
        "                trigger_pool: trigger_pool.clone(),\n"
        "                trigger_pool_match_count,\n"
        "                source: event.source.clone(),",
        "record trigger fields",
    )

    s = replace_once(
        s,
        "                wsol_amount_lamports: event.wsol_amount_lamports,\n"
        "                token_amount_raw: event.token_amount_raw,\n"
        "                pool_discovery_ok,",
        "                wsol_amount_lamports: event.wsol_amount_lamports,\n"
        "                token_amount_raw: event.token_amount_raw,\n"
        "                trigger_received_at_unix_ms,\n"
        "                tx_parsed_at_unix_ms,\n"
        "                pool_discovery_done_at_unix_ms,\n"
        "                quote_done_at_unix_ms,\n"
        "                parse_ms,\n"
        "                total_event_to_quote_ms,\n"
        "                pool_discovery_ok,",
        "record timing fields",
    )

    s = replace_once(
        s,
        '                "Event-driven V2 event #{accepted_events}: event_slot={} quote_slot={:?} mint={} direction={:?} wsol_lamports={:?} pools={} routes={} evaluated={} net_positive={} best_net_lamports={:?} discovery_ms={} quote_ms={} signature={}",\n'
        "                event.slot,\n"
        "                quote_snapshot_slot,\n"
        "                event.token_mint,",
        '                "Event-driven V2 event #{accepted_events}: event_slot={} quote_slot={:?} mint={} direction={:?} wsol_lamports={:?} trigger_pool={:?} pools={} routes={} evaluated={} net_positive={} best_net_lamports={:?} parse_ms={} discovery_ms={} quote_ms={} total_ms={} signature={}",\n'
        "                event.slot,\n"
        "                quote_snapshot_slot,\n"
        "                event.token_mint,",
        "event log format",
    )

    s = replace_once(
        s,
        "                event.wsol_amount_lamports,\n"
        "                pools.len(),\n"
        "                route_count,",
        "                event.wsol_amount_lamports,\n"
        "                trigger_pool,\n"
        "                pools.len(),\n"
        "                route_count,",
        "event log trigger pool",
    )

    s = replace_once(
        s,
        "                best_profit,\n"
        "                pool_discovery_ms,\n"
        "                quote_eval_ms,\n"
        "                event.signature",
        "                best_profit,\n"
        "                parse_ms,\n"
        "                pool_discovery_ms,\n"
        "                quote_eval_ms,\n"
        "                total_event_to_quote_ms,\n"
        "                event.signature",
        "event log timing args",
    )

    test = r'''

    #[test]
    fn resolves_trigger_pool_from_enhanced_account_data() {
        let transaction = json!({
            "accountData": [
                {"account": "user"},
                {"account": "ray-trigger"},
                {"account": "vault"}
            ]
        });
        let pools = vec![PoolInfo {
            dex: Dex::Raydium,
            address: "ray-trigger".into(),
            pool_type: "Standard".into(),
            program_id: Some(RAYDIUM_AMM_V4_PROGRAM_ID.into()),
            mint_a: "M".into(),
            mint_b: WSOL.into(),
            tvl_usd: 10_000.0,
        }];
        let (pool, count) = resolve_trigger_pool(&transaction, &pools);
        assert_eq!(pool.as_deref(), Some("ray-trigger"));
        assert_eq!(count, 1);
        assert_eq!(
            trigger_dex_for_program(RAYDIUM_AMM_V4_PROGRAM_ID),
            Some("raydium")
        );
    }
'''
    s = replace_once(
        s,
        "\n    #[test]\n    fn signature_deduper_is_bounded() {",
        test + "\n    #[test]\n    fn signature_deduper_is_bounded() {",
        "trigger-pool test",
    )

    SRC.write_text(s)
    return True


def patch_smoke() -> bool:
    s = SMOKE.read_text()
    original = s
    s = s.replace("EVENT_LOG_PATH: /tmp/event-driven-v2.jsonl", "EVENT_LOG_PATH: /tmp/large-swap-event-v1.jsonl")
    s = s.replace('assert row["schema"] == "event-driven-v2"', 'assert row["schema"] == "large-swap-event-v1"')
    if 'assert row["trigger_dex"]' not in s:
        anchor = '          assert row["event_slot"] > 0\n'
        extra = '''          assert row["event_slot"] > 0
          assert row["trigger_dex"] in {"raydium", "orca", "meteora_dlmm"}
          assert row["trigger_received_at_unix_ms"] <= row["tx_parsed_at_unix_ms"] <= row["pool_discovery_done_at_unix_ms"] <= row["quote_done_at_unix_ms"]
          assert row["total_event_to_quote_ms"] >= row["parse_ms"]
          candidate_addresses = {pool["address"] for pool in row["candidate_pools"]}
          if row["trigger_pool"] is not None:
              assert row["trigger_pool_match_count"] == 1
              assert row["trigger_pool"] in candidate_addresses
          for opp in row["opportunities"]:
              expected = None if row["trigger_pool"] is None else (
                  opp["first_pool"] == row["trigger_pool"] or opp["second_pool"] == row["trigger_pool"]
              )
              assert opp["route_contains_trigger_pool"] == expected
'''
        if anchor not in s:
            raise SystemExit("smoke assertion anchor missing")
        s = s.replace(anchor, extra, 1)
    s = s.replace("/tmp/event-driven-v2.jsonl", "/tmp/large-swap-event-v1.jsonl")
    SMOKE.write_text(s)
    return s != original


changed = patch_source()
changed |= patch_smoke()
print(f"large-swap schema patch changed={changed}")
