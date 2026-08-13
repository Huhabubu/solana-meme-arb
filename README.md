# solana-meme-arb

一个使用 Rust 开发的 Solana 成熟 Meme 跨 DEX 套利研究项目。

当前原则：**真实监控、真实验证、先研究后交易**。只有当监控数据证明机会具备可执行价值后，才进入钱包、交易构造和实盘执行。

## 当前进度

**当前阶段：V3 — 跨池套利机会计算、成本建模、记录与统计**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | 仓库、Rust 工程骨架、GitHub Actions CI | ✅ 已完成并验证 |
| V0 | BONK / WIF 跨 DEX 池发现与链上账户核验 | ✅ 已完成并验证 |
| V1 | Helius RPC/WSS 实时账户订阅 | ✅ 已完成并验证 |
| V2 | DEX Pool State、本地 Swap Quote、依赖账户实时触发 | ✅ 已完成并验证 |
| V3 | 跨池闭环、多金额利润曲线、成本、实时机会统计 | 🔄 当前进行 |
| V4 | 原子交易构造与 `simulateTransaction` | ⏳ 未开始 |
| V5 | 小额实盘执行与 Jito 集成 | ⏳ 未开始 |

## V3 当前子模块

| 子模块 | 状态 | 证据 |
|---|---|---|
| 统一 `SwapQuote` / 两腿闭环 | ✅ | Mint/金额连续性、同池拒绝、signed profit、slot 范围均有测试 |
| Raydium Standard 双方向 Quote | ✅ | `WSOL ↔ Token` 真实主网验证 |
| Orca Whirlpool 双方向 Quote | ✅ | `WSOL ↔ Token` + 官方 core Quote 真实验证 |
| Meteora DLMM 双方向 Quote | ✅ | `WSOL ↔ Token` + 官方 Rust Quote 真实验证 |
| 三 DEX 全部有向两池组合 | ✅ | 每 Token 3 池 → 6 路；BONK/WIF 共 12 路 |
| 多输入金额利润曲线 | ✅ | 0.01 / 0.05 / 0.1 SOL，同一腿快照批量本地 Quote |
| 高精度收益率 | ✅ | `gross_return_ppm` 保留小于 1 bps 的符号和量级 |
| 不足流动性点 | ✅ | 明确标记 `insufficient_liquidity`，不伪装成亏损或程序成功报价 |
| 执行成本 / 净利润模型 | 🔄 下一模块 | Priority Fee / Jito Tip / 固定执行成本尚未加入 |
| 实时 Opportunity Engine | ⏳ | 尚未把 WSS 更新直接接到全路径利润重算 |
| 机会持久化与统计 | ⏳ | 尚未进入 24–72h 连续采样 |

> 当前 V3 仍然是**研究阶段**。没有钱包、没有私钥、没有下单逻辑，也没有证据证明策略已经可盈利。

## 当前可报价研究池

目前每个 Token 先固定一个已完整支持的 Pool 类型，共 6 个池：

| Token | DEX | Pool |
|---|---|---|
| BONK | Raydium Standard | `HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv` |
| BONK | Orca Whirlpool | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` |
| BONK | Meteora DLMM | `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp` |
| WIF | Raydium Standard | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` |
| WIF | Orca Whirlpool | `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1` |
| WIF | Meteora DLMM | `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V` |

V0 仍保留更大的候选池集合用于发现/核验。Raydium CLMM 与 Meteora DAMM v2 尚未进入本地 Quote 引擎，因此不会把它们假装成已支持路径。

## V3 成功标准

V3 只有满足以下条件才标记完成：

1. 同 Token 的不同可报价池可以用统一接口计算真实本地 Quote。
2. 建立 `WSOL → Token → WSOL` 两腿闭环，而不是只比较表面价格。
3. 多个输入规模在可解释的一致快照上评估 Price Impact。
4. 明确区分 DEX fee 后的闭环毛利润与 Priority Fee / Jito Tip 等执行成本后的净利润。
5. 流动性不足必须成为显式状态，不能当成普通亏损，也不能让整个监控器崩溃。
6. WSS 依赖账户变化后，只重算受影响池与相关 Token 路径。
7. 机会记录至少包含时间/slot、Token、路径、金额、两腿输出、毛/净利润、成本和流动性状态。
8. 每个新增计算函数有单元测试；关键链路继续用真实主网做端到端验证。
9. 连续监控数据证明系统能够稳定发现、记录并解释机会后，才讨论 V4。

## 已验证结果

### Stage 0

首次完整 CI 已在真实 GitHub-hosted Ubuntu runner 上通过：`fmt / check / clippy / test`。首次真实 runner Rust 为 `rustc 1.97.1`。

### V0 — Pool Discovery

最终验收 Run `31673566193`：

- **23 passed / 0 failed**。
- BONK/WSOL 发现 117 个精确交易对池；WIF/WSOL 发现 98 个。
- 当前研究筛选规则：`MIN_MONITOR_TVL_USD = 1,000`、`MAX_POOLS_PER_DEX = 3`。
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
- Raydium Standard AMM v4：解析 752 字节 Pool State、两个 vault、`need_take_pnl`、整数 fee/constant-product Quote。
- Orca Whirlpool：使用官方 `orca_whirlpools_client 8.0.0` 与 `orca_whirlpools_core 2.1.1`，读取 Whirlpool/TickArray/Mint/可选 Oracle。
- Meteora DLMM：使用官方 Rust `commons` SDK（固定 revision `fb02e51ae677bbd18e76543f702dae40632426db`），读取 LbPair/BinArray/Mint/bitmap 并调用官方 `quote_exact_in`。
- 构建 6 个可报价池的依赖集合，去重后订阅 **31 个报价依赖账户**，其中 22 个是非 Pool 触发账户。
- 真实 Orca TickArray 更新成功映射到正确池并触发 Quote 重算。

### V3.1 — 统一闭环

最终验收 Run `31691663172`：

- **82 passed / 0 failed**。
- 新增 `SwapQuote`、`RoundTripOpportunity`、`evaluate_round_trip`。
- 严格校验两腿 Mint 连续、金额连续、最终回到原始资产、不同 Pool。
- 利润使用 signed `i128`，亏损不会发生无符号下溢。
- Raydium ↔ Orca 的 BONK/WIF 双方向共 4 条真实主网闭环全部执行成功。
- 当次 4 条均为负收益。

### V3.2 — 三 DEX 全路径

最终验收 Run `31692193766`：

- **83 passed / 0 failed**。
- 新增 `directed_route_indices`；3 个池严格生成 `3 × 2 = 6` 条有向路径。
- Meteora DLMM 接入统一双方向 Quote。
- BONK/WIF × Raydium/Orca/Meteora 共 **12 条真实闭环**全部运行成功。
- 当次 12/12 均为负收益；最接近平衡的两个 BONK 点仍然是确定的负数，未计执行成本。

### V3.3 — 一致快照多金额利润曲线

最终验收 Run `31694234098`，已读取完整 Job 日志，不只依据绿色状态：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- **89 passed / 0 failed** ✅
- V0 Pool Discovery / 主网 owner 回归 ✅
- V1 Helius HTTP/WSS 回归 ✅
- Raydium / Orca / Meteora 单池真实 Quote 回归 ✅
- V3 all-DEX multi-size round trip ✅
- V2 dependency WSS routing 回归 ✅

#### 多金额设计

当前 probe：

- 0.01 SOL = `10,000,000` lamports
- 0.05 SOL = `50,000,000` lamports
- 0.10 SOL = `100,000,000` lamports

每一腿先抓取一份一致的链上状态快照，再在本地用**同一快照**计算多个金额，避免把几秒内的市场变化误当成 Price Impact。

`RoundTripOpportunity` 同时保存：

- `gross_profit_raw`：权威 raw 盈亏值；
- `gross_return_bps`：基点级展示；
- `gross_return_ppm`：百万分比展示，可保留小于 1 bps 的符号与量级；
- `oldest_slot / newest_slot`：两腿快照范围。

#### 36 个 probe 点最终结果

12 条路径 × 3 个金额 = **36 个点全部被明确结算**：

- **34 个点完整报价并计算闭环利润**；
- **2 个点明确标记 `insufficient_liquidity`**；
- **0 个正收益点**。

两个不可完整成交点均为 WIF、0.1 SOL，且 Meteora DLMM 位于第二腿：

- Raydium → Meteora DLMM：0.1 SOL 第二腿流动性不足；
- Orca → Meteora DLMM：0.1 SOL 第二腿流动性不足。

对应路径的 0.01 / 0.05 SOL 仍然可以完整 Quote，因此程序不会因为单个大金额不可成交而丢弃整条路径。

#### 当次利润曲线示例

BONK `Meteora DLMM → Orca`：

| 输入 | gross profit | gross return ppm |
|---:|---:|---:|
| 0.01 SOL | -2,168 lamports | -216 ppm |
| 0.05 SOL | -11,982 lamports | -239 ppm |
| 0.10 SOL | -65,671 lamports | -656 ppm |

WIF `Raydium → Orca`：

| 输入 | gross profit | gross return ppm |
|---:|---:|---:|
| 0.01 SOL | -5,381 lamports | -538 ppm |
| 0.05 SOL | -28,412 lamports | -568 ppm |
| 0.10 SOL | -60,633 lamports | -606 ppm |

这两个例子都显示输入变大后收益率进一步恶化，符合 Price Impact 增大的预期；但这是单次真实快照，不作为长期统计结论。

> **截至 V3.3 仍没有观察到正毛利润点。** 而且当前还没有扣 Priority Fee / Jito Tip 等执行成本，因此更不能声称存在可执行套利机会。

#### V3.3 最后实时回归

同一 Run 最后一关真实执行：

- WSS 订阅 31 个依赖账户；
- 收到 Orca TickArray `8rEM7SiZRaSZLHU6ouo4NULBj7ZRcRYTvib2B16TqNFG` 更新；
- `slot=439007033`；
- 正确映射到 BONK Orca Pool `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`；
- 更新后真实重算 Quote；
- 最终输出 `V2 dependency-triggered quote recompute verified; refreshed dependency set is complete`。

## CI / 开发基础设施

### Cargo 缓存

- `Cargo.lock` 已提交并锁定依赖解析结果。
- CI 使用 `actions/cache@v5` 缓存 `~/.cargo/registry`、`~/.cargo/git`、`target`。
- cache key 绑定 runner OS、Rust 版本、`Cargo.toml`、`Cargo.lock`。
- 当前缓存约 **958 MB**；恢复/解压通常二十多秒，但相比约 792 个 package 的冷编译仍明显节省 Actions 时间。
- 同一分支快速连续提交时，`concurrency` 会取消旧的进行中 CI。
- README 纯文档更新不触发完整 Rust CI。

## 已遇到并解决的问题

### Raydium API 字段变化

真实 API 使用 `mintA / mintB`，旧测试样本最初写成 `mint1 / mint2`。修复解析器后增加回归测试，旧结构不会被静默接受。

### 大量灰尘池

Discovery 会返回很多 TVL 极低的池。当前用 TVL 下限和每 DEX 数量上限控制研究期 WSS/RPC 成本；该阈值不是最终交易阈值。

### API 地址不等于链上已验证

V0 后续增加 `getMultipleAccounts`：Pool 必须真实存在且 owner 与预期 Program 一致，才进入候选集合。

### V1 Rustls CryptoProvider panic

第一次真实 Helius WSS 在 TLS 初始化时 panic。显式启用 Rustls `ring` CryptoProvider；没有关闭 TLS 校验或忽略 panic。

### V2 只订阅 Pool Account 会漏 Quote 变化

Swap Quote 依赖 vault、TickArray、BinArray 等账户。V2 建立 `Pool → dependency accounts → reverse index → QuoteState`，并用真实 TickArray 更新验证。

### V2 曾出现“CI 绿色但测试其实没跑”的假阳性

旧入口不认识 `dependency-wss-check`，只打印 Usage 后退出码 0。后来：

- 主入口统一走 `app::run()`；
- `AppCommand` 明确路由命令；
- 未知命令返回错误；
- 增加命令解析回归测试；
- 关键 live step 必须人工核对日志是否真的执行目标逻辑。

因此 Run `31687579494` 才是 V2 的最终有效验收。

### `minContextSlot` 节点落后

真实 CI 多次遇到 Solana RPC `-32016: Minimum context slot has not been reached`。

当前处理：

- **不降低 `minContextSlot`**；
- 不回退读取旧状态；
- `fetch_accounts` 只对 `-32016 + 已设置 minContextSlot` 做最多 3 次有限重试；
- 第 1/2 次等待 200ms / 400ms；
- 其他 RPC / HTTP / JSON 错误立即失败。

重试边界和错误码提取均有独立测试。

### V3 小于 1 bps 被整数 bps 截断

保留 `gross_profit_raw` 作为权威值，并新增 `gross_return_ppm`。例如 -120 lamports / 0.01 SOL 对应 bps 会显示 0，但 ppm 能保留为负数，不再产生视觉误判。

### V3 Meteora 大金额 `Pool out of liquidity`

多金额测试真实暴露 WIF 某些 0.1 SOL 路径无法在 Meteora 第二腿完整成交。

当前只把 Meteora 官方错误链中精确的 `Pool out of liquidity` 分类成 `insufficient_liquidity`；其他 Quote 错误继续失败。这样：

- 不把“无法完整成交”伪装成普通亏损；
- 不因为一个大金额不可成交而让整个 Opportunity Engine 崩溃；
- 也不会吞掉真正的解析/RPC/数学错误。

该分类有单元测试。

### V3 开发中的 Clippy / one-shot workflow 问题

- 新模型曾因只被测试使用而触发 dead code；没有 `allow(dead_code)`，而是接入真实生产验收路径。
- 一次性 runner 无权修改正式 workflow；没有扩大 Token 权限，业务代码与正式 CI 分开提交。
- `clone_on_copy`、冗余 `.into_iter()` 等 lint 均按 Clippy 提示实修，没有屏蔽警告。
- 所有一次性 workflow 验证后均删除，不保留临时基础设施。

## 当前问题 / 风险

- 当前开发主要依赖 GitHub Actions，因为本次 ChatGPT 执行环境没有可直接使用的 Rust toolchain。
- `actions/checkout@v4` 仍有 Node.js 20 弃用警告；runner 当前强制 Node 24，未影响本项目验证。
- `HELIUS_API_KEY` 没有写入仓库或 Git 历史，仅通过 GitHub Actions Secret 注入。
- DEX REST API 的 TVL 是动态外部数据，不是链上实时成交价格。
- 当前完整 Quote Engine 只覆盖 Raydium Standard / Orca Whirlpool / Meteora DLMM；Raydium CLMM、Meteora DAMM v2 尚未支持。
- Orca 当前实池均为 classic SPL Token 且非 Adaptive Fee；Token-2022 transfer fee 与 Adaptive Fee Oracle 尚无当前实池验证证据。
- Meteora 0.1 SOL WIF 第二腿已经出现实际流动性上限；后续金额优化必须把“可完整成交”作为硬约束。
- **当前仍无正毛利润证据，更无扣除执行成本后的净利润证据。**
- 当前 GitHub App 无权读取个人 Billing Usage API；Actions 剩余额度需要从 GitHub `Settings → Billing & Licensing` 查看。

## 下一步

V3 下一小模块：**执行成本 / 净利润模型**。

1. 定义独立 `ExecutionCost`，至少包含 Priority Fee、Jito Tip、其他固定 lamports 成本。
2. DEX swap fee 已经包含在每腿 Quote 输出中，成本模型不得重复扣 DEX fee。
3. 新增 `net_profit_raw = gross_profit_raw - execution_cost_lamports`，保持 signed `i128`。
4. 成本模型每个函数配单元测试：零成本、正常成本、成本大于毛利润、非法/溢出边界。
5. 将成本模型接入 12 条路径 × 多金额真实检查，但在 V4 之前不假装已经知道“真实最优 Jito Tip”；先把成本来源和假设字段分开。
6. 随后把 WSS dependency update 接到 Opportunity Engine，只重算受影响池/Token。
7. 再做机会持久化与 24–72h 连续采样；需要长跑时转 Linux VPS，不把 GitHub Actions 当长期服务器。

## 开发与记录约定

- README、项目说明和代码注释使用中文；Rust 变量/函数/结构体/模块名保持英文。
- 注释重点解释“为什么”“链上字段语义”“风险与假设”，不逐行翻译显而易见代码。
- **每个有实际逻辑的函数设计对应测试；依赖真实外部服务的函数增加端到端真实测试。**
- 每完成一个阶段或有真实 blocker，立即更新 README。
- 绿色 CI 只是信号；关键端到端步骤必须核对日志是否真的执行目标逻辑。
- 只有实际运行、实际编译、实际链上数据或官方资料能够支持的结论才标记为已验证。

## 安全规则

仓库不会保存：

- Helius API Key
- 钱包私钥
- 助记词
- 任何其他交易签名密钥

本地 `.env` 必须保留在 `.gitignore`；仓库只保留不含真实密钥的 `.env.example`。

## 最近更新

**2026-08-13**

- Stage 0 / V0 / V1 / V2 已完成并真实验证。
- V3.1：统一 Quote 与 Raydium↔Orca 闭环，Run `31691663172`，82 tests。
- V3.2：三 DEX 12 条有向闭环，Run `31692193766`，83 tests。
- V3.3：同一腿快照多金额曲线、高精度 ppm、有限 `-32016` 重试、流动性不足点建模。
- V3.3 最终 Run `31694234098`：**89 passed / 0 failed；12 routes；36 points accounted = 34 evaluated + 2 insufficient-liquidity；0 positive gross-profit points**。
- 最后依赖 WSS 回归真实收到 Orca TickArray 更新并重算 Quote。
- **V3 继续进行，下一模块为执行成本 / 净利润模型。**
