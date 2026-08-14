# solana-meme-arb

一个使用 Rust 开发的 Solana 成熟 Meme 跨 DEX 套利研究项目。

当前原则：**真实监控、真实验证、先研究后交易**。只有当监控数据证明机会具备可执行价值后，才进入钱包、交易构造和实盘执行。

## 当前进度

**当前阶段：V3 — 跨池套利机会计算、成本建模、实时重算、记录与统计**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | 仓库、Rust 工程骨架、GitHub Actions CI | ✅ 已完成并验证 |
| V0 | BONK / WIF 跨 DEX 池发现与链上账户核验 | ✅ 已完成并验证 |
| V1 | Helius RPC/WSS 实时账户订阅 | ✅ 已完成并验证 |
| V2 | DEX Pool State、本地 Swap Quote、依赖账户实时触发 | ✅ 已完成并验证 |
| V3 | 跨池闭环、多金额利润曲线、成本、实时机会统计 | 🔄 当前进行：代码层监控器已具备常驻条件，等待 24–72 小时连续采样 |
| V4 | 原子交易构造与 `simulateTransaction` | ⏳ 未开始 |
| V5 | 小额实盘执行与 Jito 集成 | ⏳ 未开始 |

### V3 子模块

| 子模块 | 状态 | 当前证据 |
|---|---|---|
| 统一 `SwapQuote` / 两腿闭环 | ✅ | Mint/金额连续性、同池拒绝、signed profit、slot 范围均有测试 |
| Raydium Standard 双方向 Quote | ✅ | `WSOL ↔ Token` 真实主网验证 |
| Orca Whirlpool 双方向 Quote | ✅ | `WSOL ↔ Token` + 官方 core Quote 真实验证 |
| Meteora DLMM 双方向 Quote | ✅ | `WSOL ↔ Token` + 官方 Rust Quote 真实验证 |
| 三 DEX 全部有向两池组合 | ✅ | 每 Token 3 池 → 6 路；BONK/WIF 共 12 路 |
| 多输入金额利润曲线 | ✅ | 0.01 / 0.05 / 0.1 SOL，同一腿快照批量本地 Quote |
| 高精度收益率 | ✅ | `gross_return_ppm` 保留小于 1 bps 的符号和量级 |
| 不足流动性状态 | ✅ | 明确标记 `insufficient_liquidity`，不伪装成亏损或成功 Quote |
| 执行成本 / 净利润模型 | ✅ | `ExecutionCost → NetOpportunity`，已接入真实 12 路多金额检查 |
| RPC 短暂限流恢复 | ✅ | 429/408/部分 5xx 有界退避；`minContextSlot` 一致性约束不降低 |
| V3.5.1 affected-route 实时路由 | ✅ | WSS 更新 → affected pool → 相关 Token routes → 结构化 `OpportunityEvent` 已真实验证 |
| V3.5.2 本地 Quote Context 缓存 | ✅ | 只刷新 affected Pool Context；related routes 两腿均从本地 Context Quote，正式 E2E 已验证 |
| V3.6.1 JSONL 持久化与统计 | ✅ | 真实 OpportunityEvent 追加写盘、严格重读、分组汇总与行数核验均已通过 |
| V3.6.2 短时连续监控循环 | ✅ | 同一进程、同一 WSS session 连续处理 2 条真实 update，并持续追加同一 JSONL |
| V3 长时采样 | 🔄 下一步 | Linux VPS 连续运行 24–72 小时，形成真实机会频率/持续时间/利润分布样本 |

> 当前仍然是**研究阶段**：没有钱包、没有私钥、没有下单逻辑，也没有证据证明策略已经可盈利。

## 当前研究 Universe

当前用 BONK / WIF 作为两个并行测试样本。套利逻辑本身不与这两个 Token 绑定；后续会把 Token 集合改成动态 Universe。

目前每个 Token 先固定一个已完整支持的 Pool 类型，共 6 个可报价研究池：

| Token | DEX | Pool |
|---|---|---|
| BONK | Raydium Standard | `HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv` |
| BONK | Orca Whirlpool | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` |
| BONK | Meteora DLMM | `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp` |
| WIF | Raydium Standard | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` |
| WIF | Orca Whirlpool | `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1` |
| WIF | Meteora DLMM | `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V` |

V0 仍保留更大的候选池集合用于发现/核验。Raydium CLMM 与 Meteora DAMM v2 尚未进入本地 Quote 引擎，因此不会把它们算作当前已支持路径。

## V3 成功标准

V3 只有满足以下条件才标记完成：

1. 同 Token 的不同可报价池可以用统一接口计算真实本地 Quote。
2. 建立 `WSOL → Token → WSOL` 两腿闭环，而不是只比较表面价格。
3. 多个输入规模在可解释的一致快照上评估 Price Impact。
4. 明确区分 DEX fee 后的闭环毛利润与 Priority Fee / Jito Tip 等执行成本后的净利润。
5. 流动性不足必须成为显式状态，不能当成普通亏损，也不能让整个监控器崩溃。
6. WSS 依赖账户变化后，只重算受影响池与相关 Token 路径。
7. 机会记录至少包含时间/slot、Token、路径、金额、两腿输出、毛/净利润、成本和流动性状态。
8. 每个新增计算函数有单元测试；关键链路继续用真实主网端到端验证。
9. 连续监控数据证明系统能够稳定发现、记录并解释机会后，才讨论 V4。

## 已验证结果

### Stage 0

GitHub-hosted Ubuntu runner 上 `fmt / check / clippy / test` 已通过。当前 CI 使用 Rust stable；已验证过 `rustc 1.97.1`。

### V0 — Pool Discovery

最终验收 Run `31673566193`：

- **23 passed / 0 failed**。
- BONK/WSOL 发现 117 个精确交易对池；WIF/WSOL 发现 98 个。
- 研究筛选规则：`MIN_MONITOR_TVL_USD = 1,000`、`MAX_POOLS_PER_DEX = 3`。
- BONK/WIF 共 18 个候选 Pool Account：**18/18 链上存在，18/18 owner 与预期 DEX Program 一致**。

### V1 — Helius HTTP/WSS

最终验收 Run `31675468153`：

- **34 passed / 0 failed**。
- Helius HTTP `getVersion` 真实通过。
- 18 个候选 Pool Account 的 WSS `accountSubscribe` 全部确认。
- 收到真实 Pool Account 更新，并通过 `subscription id ↔ PoolInfo` 映射回正确 DEX/Pool。
- `HELIUS_API_KEY` 仅由 GitHub Actions Secret 注入，日志中显示为 `***`。

### V2 — 三个本地 Quote 引擎

最终有效验收 Run `31687579494`：

- **78 passed / 0 failed**。
- Raydium Standard AMM v4：解析 Pool State + vault，按程序规则计算本地 exact-input Quote。
- Orca Whirlpool：使用官方 client/core，读取 Whirlpool/TickArray/Mint/可选 Oracle。
- Meteora DLMM：使用官方 Rust SDK，读取 LbPair/BinArray/Mint/bitmap 并调用官方 `quote_exact_in`。
- 6 个可报价池去重后订阅 **31 个报价依赖账户**，其中 22 个是非 Pool 触发账户。
- 真实 Orca TickArray 更新成功映射到正确池并触发 Quote 重算。

### V3.1 — 统一闭环

最终验收 Run `31691663172`：

- **82 passed / 0 failed**。
- 新增 `SwapQuote`、`RoundTripOpportunity`、`evaluate_round_trip`。
- 严格校验两腿 Mint 连续、金额连续、最终回到原始资产、不同 Pool。
- 利润使用 signed `i128`，亏损不会无符号下溢。
- BONK/WIF 的 Raydium ↔ Orca 共 4 条真实闭环全部执行成功。

### V3.2 — 三 DEX 全路径

最终验收 Run `31692193766`：

- **83 passed / 0 failed**。
- 3 个池生成 `3 × 2 = 6` 条有向路径。
- BONK/WIF × Raydium/Orca/Meteora 共 **12 条真实闭环**运行成功。

### V3.3 — 一致快照多金额利润曲线

最终验收 Run `31694234098`：

- **89 passed / 0 failed**。
- Probe：0.01 / 0.05 / 0.10 SOL。
- 每一腿先抓一份一致链上状态，再在本地用同一份状态计算多个金额。
- `gross_profit_raw` 是权威 raw 盈亏；另保存 `gross_return_bps`、`gross_return_ppm`、`oldest_slot/newest_slot`。
- 当次 36 个 probe 点全部被明确结算：**34 evaluated + 2 insufficient_liquidity**。
- 当次 **0 gross-positive**。

流动性不足是动态池状态。程序不会把单个不可完整成交金额伪装成负收益，也不会因此丢弃整条路径。

### V3.4 — 执行成本与净利润

最终验收 Run `31763729071`，已人工读取完整 Job 日志核对：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- **95 passed / 0 failed** ✅
- V0 Pool Discovery / owner 回归 ✅
- V1 Helius HTTP/WSS ✅
- Raydium / Orca / Meteora 单池真实 Quote ✅
- V3 12 路多金额闭环 + 成本模型 ✅
- 最终 dependency WSS 更新 → 正确 Pool → Quote 重算 ✅

新增模型：

```text
RoundTripOpportunity
        ↓
ExecutionCost
        ↓
NetOpportunity
```

`ExecutionCost` 明确拆分：

- `base_fee_lamports`
- `priority_fee_lamports`
- `jito_tip_lamports`
- `other_lamports`

DEX swap fee 已经反映在两腿 Quote 输出里，因此不会在 `ExecutionCost` 中重复扣除。

#### 当前 CI 的 6000-lamport“成本下界”

V3.4 为了验证净利润链路，使用一个**研究成本下界**：

- Base fee：5,000 lamports（当前假设一笔原子交易只需要 1 个普通签名）。
- Priority fee：0（暂未假装知道真实 CU 与竞争性 CU price）。
- Jito tip：1,000 lamports（只取当前文档最低 tip）。
- Other：0。
- 合计：**6,000 lamports**。

> 这不是实盘真实 landing cost。Priority Fee 和竞争性的 Jito Tip 必须等 V4 构造出真实交易、得到 CU 消耗与当时市场报价后动态估计。

Run `31763729071` 的真实主网快照：

- 12 条路径 × 3 个金额 = 36 个 probe 点。
- **32 evaluated + 4 insufficient_liquidity = 36/36 accounted**。
- **0 gross-positive**。
- **0 positive under 6000-lamport cost floor**。
- 4 个不可完整成交点均来自 WIF、Meteora 第二腿：Raydium→Meteora 和 Orca→Meteora 的 0.05 / 0.10 SOL。

示例：BONK `Orca → Meteora DLMM`，0.01 SOL：

- gross profit = `-5,643` lamports
- cost floor = `6,000` lamports
- net profit floor = `-11,643` lamports

当次依然没有发现可执行套利证据。

#### RPC 429 修复

V3.4 第一轮正式 E2E Run `31763274831` 已经真实输出毛利润→成本→净利润，但在后段 Helius full-account 请求收到 HTTP `429 Too Many Requests`，因此该 Run **不作为完成证据**。

处理后 `fetch_accounts` 对短暂 HTTP 错误增加独立、有界退避：

- 可重试：408 / 429 / 500 / 502 / 503 / 504。
- 最多 4 次重试（首次请求加 4 次重试，共最多 5 次请求机会）。
- 无 `Retry-After` 时：1s → 2s → 4s → 8s；上限 30s。
- 有合法 `Retry-After` 时优先采用，并限制到 30s。
- 400 / 401 / 403 / 404 / 409 / 422 等确定性错误不重试。
- 原有 `-32016 Minimum context slot has not been reached` 仍使用独立的 200ms / 400ms 有限重试。
- 两套重试都复用原请求，**不会降低 `minContextSlot`，不会为了成功而读取更旧状态**。

新增两组 HTTP retry 单元测试后，正式测试数从 93 增到 **95**。

### V3.5.1 — WSS affected-route Opportunity Engine

最终验收 Run `31764204176`，已人工读取完整 Job 日志核对：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- **100 passed / 0 failed** ✅
- V0/V1/V2/V3.4 全部真实回归 ✅
- `opportunity-wss-check` 真实执行 ✅

新增：

- `DirectedPoolRoute`：表示同一 Token 下有方向的两池闭环。
- `affected_directed_pool_routes`：只生成第一腿或第二腿包含 affected pool 的相关路径，并跨 Token 分组隔离、去重。
- `OpportunityEvent`：结构化记录一条金额 probe 的结果。
- `OpportunityEventOutcome::Evaluated`：保存两腿输出、gross/net profit、成本和 slot 范围。
- `OpportunityEventOutcome::InsufficientLiquidity`：显式保存 `FirstLeg / SecondLeg` 流动性不足阶段。

5 组新增单元测试覆盖：单池 affected route、多个 affected pool 去重、未知 pool/错误 base 拒绝、evaluated event 字段一致性、流动性不足显式状态。

#### 真实 WSS 触发

本次收到的真实更新：

- dependency account：`So11111111111111111111111111111111111111112`（WSOL Mint）。
- slot：`439140377`。
- subscription：`832759`。
- dependency kind：`TokenMint`。
- affected pools：
  - BONK Meteora DLMM `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp`
  - WIF Meteora DLMM `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V`

因为 WSOL Mint 是两个 Meteora Pool 共享依赖，本次一个 WSS update 同时映射到两个不同 Token 的 affected pool。路由器没有把两个 Token 混在一起：

- 每个 affected Meteora pool 在对应 3-pool Token 组内产生 4 条相关有向路径。
- BONK 4 条 + WIF 4 条 = **8 related routes**。
- 每条 3 个金额 probe，共 **24 OpportunityEvent**。
- **20 evaluated + 4 insufficient-liquidity = 24/24 accounted**。
- **0 net-positive**。

4 个流动性不足事件仍是 WIF 的 `Raydium → Meteora` / `Orca → Meteora` 在 0.05 / 0.10 SOL 的第二腿，不会被伪装成普通亏损。

最终日志明确输出：

```text
V3.5 dependency-triggered opportunity recompute verified:
2 affected pool(s), 8 related route(s), 24 event(s),
20 evaluated, 4 insufficient-liquidity, 0 net-positive
```

### V3.5.2 — 本地 Quote Context 缓存

最终验收 Run `31765141544`，已人工读取完整 Job 日志核对：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- **104 passed / 0 failed** ✅
- V0/V1/V2/V3.4 全部真实回归 ✅
- V3.5 本地 Context WSS E2E ✅

新增 `QuoteContextCache`，为 6 个可报价 Pool 保存可复用本地 Context：

- Raydium：已解码 AMM state + 两个 vault balance。
- Orca：Whirlpool + 5 个 TickArray facade + 可选 Oracle。
- Meteora：LbPair + 双方向 BinArray map + 可选 bitmap + 两个 Mint Account。

所有 Context variant 使用 `Box` 间接存储，避免 Orca 5 个完整 TickArray 把 enum 本体膨胀到几十 KB；这个结构问题由 Clippy 在 guarded 开发阶段实际发现后修正，没有关闭 lint。

Context cache 具备：

- 初始从 `QuoteState` 本地账户字节构建；
- `refresh_pool` 只重建 affected pool；
- context slot 不允许倒退；
- 任意输入 Mint / amount 批量本地 Quote；
- 未变化 counterpart pool 直接复用旧 Context。

Meteora 的依赖集合现在合并两个 swap 方向需要的 BinArray，因此 WSS 去重依赖从 V3.5.1 的 31 个增加到 **32 个**。Clock sysvar 仍不作为 WSS trigger；每轮 opportunity recompute 只刷新一次 Clock，再供全部相关路径复用。

#### 真实 local-context WSS E2E

Run `31765141544` 收到：

- dependency account：`2qJr7TWGCw3qdHXSfez1YftQcyqyfDVdVGbvvJNQZkPz`
- kind：`TickArray`
- slot：`439143063`
- subscription：`583450`
- affected pool：BONK Orca Whirlpool `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`

程序先把 WSS 数据写入 `QuoteState`，刷新该 Orca Pool 的动态依赖与 Context；其余 5 个 Pool Context 保持复用。随后：

- **1 affected pool**
- **4 related directed routes**
- **12 OpportunityEvent**（4 routes × 3 amounts）
- **12 evaluated / 0 insufficient-liquidity**
- **0 net-positive**

日志明确输出：

```text
V3.5 local context recompute: 1 affected pool(s), 4 related route(s);
route evaluator performs no pool snapshot HTTP requests,
Clock refreshed once at slot 439143063
```

以及最终：

```text
V3.5 local-context opportunity recompute verified:
1 affected pool(s), 4 related route(s), 12 event(s),
12 evaluated, 0 insufficient-liquidity, 0 net-positive
```

因此 V3.5 已完成从“账户更新”到“只刷新受影响 Context，再对相关路径完全本地 Quote”的闭环。

### V3.6.1 — JSONL 持久化与统计

最终验收 Run `31765617583`，Job `94660799354`，已人工读取完整日志核对：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- **108 passed / 0 failed** ✅
- V0/V1/V2/V3.4/V3.5 全部真实回归 ✅
- JSONL 文件非空、程序内重读成功、Shell 行数核验成功 ✅

新增 `src/persistence.rs`：

- `OpportunityRecord` schema version = 1。
- 记录 observation time、trigger slot/account/subscription、Token、两池 DEX/Pool、输入金额、两腿输出、gross/net、成本、slot 范围与流动性状态。
- `append_records` 使用 create + append，一条 JSON 一行，不覆盖旧记录。
- `read_records` 逐行严格解析；空行、损坏 JSON、schema/字段语义错误直接报错，不静默跳过。
- `summarize_records` 统计 total / evaluated / insufficient / gross-positive / net-positive / best net，并按 Token + route + amount 分组。

4 组新增持久化测试覆盖：Event→Record 状态映射、两次 append 后顺序重读、损坏 JSONL 显式失败、总体与分组统计。

#### 真实落盘 E2E

Run `31765617583` 最后一关：

- WSS 订阅 **32 个** Quote 依赖账户。
- trigger account：`2qJr7TWGCw3qdHXSfez1YftQcyqyfDVdVGbvvJNQZkPz`。
- kind：`TickArray`。
- slot：`439144425`。
- subscription：`603985`。
- affected pool：BONK Orca `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`。
- 1 affected pool → 4 related routes → **12 OpportunityEvent**。
- **12 evaluated / 0 insufficient / 0 net-positive**。

随后同一进程把 12 个 Event 转成 12 个 `OpportunityRecord` 并追加到：

```text
/home/runner/work/_temp/opportunity-events.jsonl
```

程序重新打开 JSONL 后汇总得到：

```text
appended=12
total=12
evaluated=12
insufficient_liquidity=0
gross_positive=0
net_positive=0
groups=12
```

最后 CI Shell 再执行文件非空检查与 `wc -l`，真实输出：

```text
V3.6 JSONL line count: 12
```

因此 V3.6.1 已经验证“真实 WSS OpportunityEvent → 追加写盘 → 严格重读 → 汇总统计”整条链路。

### V3.6.2 — 短时连续监控循环

最终验收 Run `31769969751`，Job `94673667609`，已人工读取完整日志核对：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- **114 passed / 0 failed** ✅
- V0/V1/V2/V3.4/V3.5/V3.6.1 真实回归全部通过 ✅
- 同一进程、同一 WSS session 连续处理 2 条真实 update ✅
- 同一 JSONL 持续追加，并在退出时严格重读/汇总一致 ✅

新增 `OpportunityMonitorConfig`、`UpdateWatermark` 与长连接监控控制：

- 可按成功处理 update 数或最大运行时间退出；
- 每次 `next_update()` 设独立等待上限；
- 精确重复 update 跳过；旧 slot update 跳过；同 slot 但数据变化仍接受；
- 可恢复 WSS 中断使用有限重连和 1/2/4/8… 秒退避，上限 30 秒；
- 依赖集合变化时重建固定 Universe 的 `QuoteState + QuoteContextCache + WSS session`；
- JSONL 始终使用 append，不因重连覆盖历史；
- 累计统计使用增量 ingest，退出时再从完整 JSONL replay，二者必须完全一致。

#### 真实连续 WSS E2E

本次监控连接：

```text
session=1
subscriptions=32
```

随后在**同一个 session=1** 内连续收到两条真实 Orca TickArray 更新，二者 slot 都是 `439156697`，但账户地址不同：

1. `8mHWP1h4NnCmWPDw1PvLJwWLiGCDm3waC2HUqRoTqWQh`
2. `8v3J8ZMXaVyG3fskBYyyr2WDs153X8ermyQfdE1wGPUt`

两条都映射到 WIF Orca Whirlpool `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1`，`kind=TickArray`，不需要重建动态依赖集合。

每条 update 都产生：

- 1 affected pool；
- 4 related routes；
- 12 records；
- 10 evaluated + 2 insufficient-liquidity；
- 0 gross-positive / 0 net-positive。

两条累计结果：

```text
processed_updates=2
appended_records=24
total_records=24
connected_sessions=1
reconnects=0
subscription_refreshes=0
duplicate_updates=0
stale_updates=0
max_updates_in_single_session=2
evaluated=20
insufficient=4
gross_positive=0
net_positive=0
```

CI 还额外 `grep` 强制要求 `processed_updates=2` 和 `max_updates_in_single_session=2`，因此“两次各自重连、每个 session 只处理一条”不能冒充本轮成功。最后 `wc -l` 真实输出：

```text
V3.6.2 continuous JSONL line count: 24
```

因此 V3.6.2 已经验证短时常驻监控器的核心循环。**它证明代码具备常驻条件，但两条 update 不是长期稳定性/机会分布样本，所以 V3 整体仍等待 VPS 24–72 小时采样。**

## 最新实时链路

Run `31769969751` 已真实验证：

```text
Helius WSS long-lived session
        ↓
next_update() → 去重 / stale slot 过滤
        ↓
QuoteState + affected Context refresh
        ↓
related routes 本地 Quote
        ↓
OpportunityEvent + gross/net
        ↓
OpportunityRecord
        ↓
同一 JSONL append
        ↓
增量统计
        ↓
next_update() 继续监听
        ↓
退出时 JSONL replay 与增量统计一致性核对
```

本轮实测：同一 WSS session 连续 2 条 update，累计 24 条 JSONL，20 evaluated + 4 insufficient，0 gross/net positive。

## CI / 开发基础设施

- `Cargo.lock` 已提交，依赖解析结果固定。
- `actions/cache@v5` 缓存 Cargo registry/git/target，当前归档约 **958 MB**。
- cache key 绑定 OS、Rust 版本、`Cargo.toml`、`Cargo.lock`。
- 同一分支快速连续提交时，通过 `concurrency` 自动取消旧 CI。
- 2026-08-14 一次 GitHub-hosted Runner 出现“Job 显示 in_progress 但 0 steps、updated_at 不再推进”的调度异常；最终把 concurrency group 从旧组轮换为 `ci-v2-*`，保留原有自动取消语义，同时让新 Run 脱离异常旧组。最终 Run `31769969751` 正常执行并完成。
- README / 纯文档更新不触发完整 Rust CI。
- 关键真实联网步骤仍会人工读取 Job 日志，不能只看绿色状态。
- `actions/checkout@v4` 当前存在 Node.js 20 弃用警告；GitHub runner 会以 Node 24 执行，目前不影响业务验证。

## 已遇到的重要问题

### CI 绿色但测试实际没执行

V2 曾出现未知命令只打印 Usage 且退出码为 0，导致假阳性。已改为统一命令解析，未知命令必须失败，并增加回归测试。

### Raydium API 字段变化

真实 API 使用 `mintA/mintB`，旧 fixture 使用 `mint1/mint2`。真实 CI 暴露后已修正并增加防回归测试。

### Rustls CryptoProvider

首次真实 Helius WSS 连接因 TLS CryptoProvider 未指定而 panic。已显式启用 `rustls` 的 `ring` 后端并完成真实 WSS 回归。

### `minContextSlot` 节点短暂落后

收到过 `-32016`。处理方式是保留一致性约束并有限重试，不允许回退读取旧状态。

### Helius HTTP 429

V3.4 多路径快速 full-account 请求首次触发 429。现已加入有界退避并由正式 Run `31763729071` 完整回归通过。

### V3.5.2 Context 类型与内存布局

第一版 Context Cache 在 guarded CI 中暴露两类问题：

- Orca 与 Meteora 依赖的 Solana `Pubkey` 来自不同 crate 版本，不能混用；最终明确使用 Orca `solana_pubkey::Pubkey` 与 Meteora/Anchor SDK Pubkey。
- Orca Context 内含 5 个完整 TickArray，Clippy `large_enum_variant` 拒绝通过；随后 Meteora Context 也触发同类问题。最终三个 DEX Context variant 全部统一 Box，保持 enum 小而稳定。

这些失败均发生在 guarded workflow 提交源码之前；最终只有通过 fmt/check/clippy/tests 的版本进入 `main`。

### GitHub Runner 0-step 调度异常

V3.6.2 最终验收前，一条清理提交 Run `31769688476` 被 GitHub 标成 `in_progress`，但 Job API 长时间返回 0 steps，Run 的 `updated_at` 也停止推进；后续同 concurrency group 的 Run 因此排队。

这不是 Rust monitor 超时。处理方式是仅轮换正式 CI concurrency group 到 `ci-v2-*`，不修改业务代码、不关闭并发保护。新 Run `31769969751` 随即获得正常 Runner、完整上报 steps 并全部通过。

## 当前边界 / 风险

- 当前可报价池只有 6 个；Raydium CLMM、Meteora DAMM v2 尚未接入 Quote Engine。
- Orca 当前真实池未覆盖 Token-2022 transfer fee / Adaptive Fee 的实池分支。
- Orca 尚未初始化的 TickArray PDA 按官方空数组语义可用于本地 Quote，但不存在的账户无法直接 WSS 订阅；若该 PDA 后续首次初始化，需要其他已订阅依赖更新触发动态依赖刷新后才能被纳入订阅。这一边界尚未做专门的“新 TickArray 初始化”实时测试。
- 当前所有已观察 V3 快照都没有提供正净利润证据。
- 6,000 lamports 只是 V3.4 研究成本下界，不能当成未来实盘交易成本。
- V3.6.2 已验证同一 WSS session 连续两次 update，但尚未用 24–72 小时样本验证长期连接稳定性、断线重连频率与真实机会分布。
- 连续 24–72 小时监控不会使用 GitHub Actions 常驻运行；下一步迁移 Linux VPS。

## 下一步

### V3 长时真实采样 — Linux VPS

代码层面的 V3 监控器现在已经具备常驻条件。下一步不继续用 GitHub Actions 人为拉长运行时间，而是部署到 Linux VPS 做 24–72 小时连续采样。

采样目标：

1. 验证 Helius WSS 长连接的实际稳定性、断线次数、重连次数与 subscription refresh 次数。
2. 持续记录所有 `OpportunityRecord`，而不是只记录正利润事件，避免样本选择偏差。
3. 统计 BONK/WIF × DEX route × amount 的 evaluated / insufficient / gross-positive / net-positive 次数与比例。
4. 统计正价差出现后的持续时间、连续 slot 数、最优输入规模和净利润分布。
5. 对比 0.01 / 0.05 / 0.10 SOL，判断 Price Impact 与池深度对可执行规模的限制。
6. 记录程序运行中的 RPC 429、`-32016`、WSS reconnect、stale/duplicate update 等系统指标。
7. 24–72 小时样本结束后，用 Python/pandas 对 JSONL 做离线分析，并据数据决定：继续 BONK/WIF、扩充动态 Universe，还是进入 V4。

> **只有长时样本证明系统能稳定记录并解释机会后，才把 V3 整体标记完成并讨论 V4 原子交易。**

## 开发与安全约定

- README、项目说明和代码注释使用中文；Rust 标识符使用英文。
- 每个有实际逻辑的函数设计对应测试；依赖外部服务的关键链路增加真实端到端测试。
- 绿色 CI 只是信号；关键步骤必须核对实际日志内容。
- 只有实际运行、编译、链上数据或官方资料支持的结论才标记为已验证。
- 仓库不保存 Helius API Key、钱包私钥、助记词或其他签名密钥。
- `.env` 保持在 `.gitignore`；仓库仅保留不含真实密钥的 `.env.example`。

## 最近更新

**2026-08-14**

- V3.4 `ExecutionCost / NetOpportunity` 接入真实 `round-trip-check`。
- 第一轮正式 E2E Run `31763274831` 在后段遭遇 Helius HTTP 429，因此未标记完成。
- `fetch_accounts` 新增独立、有界 transient HTTP retry；保留原有 `minContextSlot` 一致性策略。
- V3.4 最终 Run `31763729071`：**95 passed / 0 failed**，32 evaluated + 4 insufficient liquidity，0 gross-positive，0 net-positive under 6000-lamport cost floor。
- V3.5.1 新增 affected-route 选择、`DirectedPoolRoute`、结构化 `OpportunityEvent` 与显式 liquidity stage。
- V3.5.1 最终 Run `31764204176`：**100 passed / 0 failed**；真实 WSOL Mint WSS update 同时映射 BONK/WIF 两个 Meteora Pool，生成 8 related routes / 24 events，20 evaluated + 4 insufficient，0 net-positive。
- V3.5.2 新增 `QuoteContextCache`；Meteora 依赖缓存扩展到双方向 BinArray，相关 WSS 依赖达到 32 个。
- V3.5.2 最终 Run `31765141544`：**104 passed / 0 failed**；真实 Orca TickArray update 只刷新一个 affected Context，4 related routes / 12 events 完全本地重算，0 net-positive。
- V3.6.1 新增稳定 JSONL schema、append/read 与分组统计。
- V3.6.1 最终 Run `31765617583`：**108 passed / 0 failed**；真实 WSS trigger 产生 12 Event，追加 12 JSONL、重读 12、Shell 核验 12 行，0 gross/net positive。
- V3.6.2 新增长连接 monitor、去重/stale watermark、有限 WSS 重连、依赖集合变化后 session rebuild、增量统计与最终 JSONL replay 一致性检查。
- V3.6.2 最终 Run `31769969751`：**114 passed / 0 failed**；同一 session 连续处理 2 条真实 TickArray update，累计 24 JSONL，20 evaluated + 4 insufficient，0 gross/net positive；`reconnects=0`、`subscription_refreshes=0`、`max_updates_in_single_session=2`。
- 清理所有 V3.6.2 one-shot workflow；正式仓库只保留 `ci.yml`。
- **V3.6.2 完成；下一步进入 Linux VPS 24–72 小时真实采样。**