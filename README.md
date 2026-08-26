# solana-meme-arb

一个使用 Rust 开发的 Solana 跨 DEX 套利研究项目。

> 当前原则：**真实监控、真实验证、先研究后交易。** 只有长时间监控数据证明机会具有可执行价值后，才进入钱包、交易构造和实盘执行。

## 当前状态

**当前阶段：V3 — 代码与部署链路已具备常驻条件，等待 Linux VPS 24–72 小时真实采样。**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | Rust 工程、私有仓库、GitHub Actions CI | ✅ |
| V0 | BONK/WIF Pool Discovery + Mainnet owner 核验 | ✅ |
| V1 | Helius HTTP/WSS 实时账户订阅 | ✅ |
| V2 | 三 DEX Pool State + 本地 exact-input Quote + 依赖账户触发 | ✅ |
| V3 | 闭环利润、金额曲线、成本、实时重算、JSONL、常驻 monitor | 🔄 代码完成；等待 24–72h 长时样本 |
| V4 | 原子交易构造 + `simulateTransaction` | ⏳ |
| V5 | 小额实盘 + Jito | ⏳ |

### V3 子模块

| 子模块 | 状态 | 已验证内容 |
|---|---|---|
| 统一 `SwapQuote` / 两腿闭环 | ✅ | Mint/amount 连续性、同池拒绝、signed profit、slot 范围 |
| Raydium Standard 双方向 Quote | ✅ | `WSOL ↔ Token` Mainnet 实测 |
| Orca Whirlpool 双方向 Quote | ✅ | 官方 core Quote + TickArray 实测 |
| Meteora DLMM 双方向 Quote | ✅ | 官方 Rust Quote + BinArray 实测 |
| 支持池全有向路径 | ✅ | 先过滤当前 Quote Engine 支持池型，再按每 DEX TVL Top-N，生成全部有向两池路径 |
| 多金额利润曲线 | ✅ | 0.01 / 0.05 / 0.10 SOL，同一快照本地批量 Quote |
| 流动性不足 | ✅ | 显式 `insufficient_liquidity` |
| 执行成本 / 净利润 | ✅ | `ExecutionCost → NetOpportunity` |
| RPC 恢复 | ✅ | `-32016` 有限重试；429/408/部分 5xx 有界退避 |
| affected-route 实时路由 | ✅ | WSS 仅作触发；相关路径依赖刷新到同一 RPC context slot，>100 账户时自动分片并一致性重试 |
| `QuoteContextCache` | ✅ | 相关路径所有 Pool Context 使用同一个 RPC snapshot slot |
| JSONL 持久化 / 统计 | ✅ | append-only、流式重放、崩溃尾行恢复、分组统计 |
| 连续 monitor | ✅ | 长连接、去重/stale、有限重连、动态依赖刷新、正值跨 slot 复核 |
| Linux VPS 部署准备 | ✅ | systemd/env、x86_64/ARM64 release artifact、checksum、release 主网 smoke |
| 24–72h 长时采样 | 🔄 | **尚未实际执行，因此 V3 尚未整体完成** |

## 当前研究 Universe

BONK/WIF 只是两个并行测试样本，套利引擎不与这两个 Token 绑定。后续根据长时数据决定是否扩展为动态 Universe。

当前 Token Universe 仍为 BONK/WIF；Pool Universe 不再写死地址。启动时会先过滤当前 Quote Engine 支持的池型，再按每个 DEX 的 TVL 选择最多 `MAX_POOLS_PER_DEX` 个池。程序会把本次选择写入与 JSONL 同名的 `.universe` 清单；重启后若池集合变化，会拒绝继续写旧样本，要求新建 `OPPORTUNITY_LOG_PATH`。


尚未进入本地 Quote Engine：Raydium CLMM、Meteora DAMM v2 以及其他 Solana DEX/Pool 类型。

## 当前实时链路

```text
Helius WSS long-lived session
        ↓
RawAccountUpdate
        ↓
duplicate / stale slot 过滤
        ↓
WSS 只确定 affected Pool / related routes
        ↓
coherent getMultipleAccounts 刷新相关路径全部依赖与 Clock（>100 时分片并对齐同一 context slot）
        ↓
QuoteState + 相关 QuoteContext 使用同一 snapshot slot
        ↓
0.01 / 0.05 / 0.10 SOL 本地闭环 Quote
        ↓
Gross Profit → ExecutionCost → NetOpportunity
        ↓
首次一致快照先写入 OpportunityRecord
        ↓
若 net-positive，等待并用下一 slot 的一致快照复核，再追加第二组 OpportunityRecord
        ↓
append-only JSONL + 增量统计
        ↓
next_update()
```

当前没有钱包、私钥、签名或下单代码。

## 关键验收证据

| 模块 | 最终验收 Run | 单测 | 关键实证 |
|---|---:|---:|---|
| Stage 0 | `31671979016` | 1 | Rust CI 首次闭环 |
| V0 | `31673566193` | 23 | 18/18 候选 Pool Account 存在且 owner 正确 |
| V1 | `31675468153` | 34 | Helius HTTP + WSS + 真实 account update |
| V2 | `31687579494` | 78 | 三 DEX 本地 Quote + 真实依赖账户 WSS 重算 |
| V3.1 | `31691663172` | 82 | Raydium↔Orca 真实两腿闭环 |
| V3.2 | `31692193766` | 83 | BONK/WIF 共 12 条有向路径 |
| V3.3 | `31694234098` | 89 | 36 probe；34 evaluated + 2 insufficient；0 positive |
| V3.4 | `31763729071` | 95 | 成本模型；32 evaluated + 4 insufficient；0 gross/net positive |
| V3.5.1 | `31764204176` | 100 | shared WSOL Mint update → 8 routes / 24 events |
| V3.5.2 | `31765141544` | 104 | affected Context 局部刷新，route evaluator 不拉 Pool snapshot |
| V3.6.1 | `31765617583` | 108 | 12 Event → 12 JSONL → 重读/统计/行数一致 |
| V3.6.2 | `31769969751` | 114 | 同一 WSS session 连续 2 update → 24 JSONL |

### V3.6.2 连续监控实证

Run `31769969751` / Job `94673667609`：

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

两条 update 都在同一个 WSS `session=1`。这证明短时常驻循环可工作，**不能代替长期稳定性和机会分布样本**。

## 成本模型边界

当前 V3 使用一个只用于研究链路验证的 **6,000 lamports 成本下界**：

```text
Base fee       5,000
Priority fee       0
Jito tip        1,000
Other               0
────────────────────
Total            6,000 lamports
```

DEX swap fee 已体现在两腿 Quote 输出中，不重复扣除。

> 6,000 lamports **不是未来实盘 landing cost**。V4 构造真实交易后，Priority Fee 与竞争性 Jito Tip 必须根据实际 CU 与当时市场动态估计。

2026-08-14 的两小时旧样本记录过 47 条正“研究净利润”，但其中大量记录混用了不同 slot 的账户状态，不能作为可执行套利证据。修复后必须重新采样；首次一致快照与下一 slot 复核会同时保留，后续离线分析据此判断机会持续时间和可执行性。

## Linux VPS 部署准备

仓库提供 provider-neutral 部署层：

```text
deploy/
├── README.md
├── monitor.env.example
└── solana-meme-arb.service

.github/workflows/
├── ci.yml
└── release-build.yml
```

`systemd` 服务使用专用无登录用户 `solana-arb`，Helius Key 从 `/etc/solana-meme-arb/monitor.env` 注入，JSONL 写入 `/var/lib/solana-meme-arb/`，本阶段不配置钱包或签名材料。

详细命令见 `deploy/README.md`。

## Release 构建与部署验收

生产采样不在 VPS 上现编 Rust 依赖。`release-build` 使用原生 GitHub-hosted runner分别生成 Linux x86_64 和 ARM64/aarch64 artifact。

每个架构现在都必须执行：

```text
cargo build --release --locked
        ↓
cargo test --release --locked --all-targets
        ↓
直接运行 target/release/solana-meme-arb
        ↓
真实 Helius WSS / Mainnet 1-update smoke
        ↓
峰值 RSS 测量
        ↓
file 架构断言 + SHA256SUMS
        ↓
Actions artifact
```

### 双架构 artifact / checksum 验收

Run `31771245684`，commit `48e3e51c14996a3374c9f824f9220d63e1380057`：

- x86_64 与 ARM64 均 native release build ✅
- 两边 release tests：**114 passed / 0 failed** ✅
- `file` 分别确认 x86-64 / ARM aarch64 ✅
- 两个 artifact 均实际下载、解压并执行 `sha256sum -c SHA256SUMS` → `solana-meme-arb: OK` ✅

### 最终 release binary 主网 smoke + 内存实测

Run `31771794143`，commit `72ef71ddea1d910a35b47d02333b870835dd8ad7`：

#### x86_64

- release tests：**114 / 114**
- 最终 `target/release/solana-meme-arb opportunity-monitor` 直接运行
- Helius WSS subscriptions：32
- 真实 trigger：WSOL Mint，slot `439161856`
- 2 affected Meteora pools → 8 routes → **24 JSONL records**
- 20 evaluated + 4 insufficient
- `processed_updates=1`
- `reconnects=0`
- 峰值 RSS：**21,304 KB（约 20.8 MiB）**
- release smoke wall time：22.38 秒
- binary SHA256：`21c35aa1bf1f05e19809a44440bd783595fa6119aac3418f9e523daf358c805e`
- artifact ID：`9208425707`
- artifact size：`3,740,179` bytes
- artifact digest：`sha256:8bda9b9bdef5b13c8cf685dd0d6bea474ad8d10b3ea942fd3641630a950ad168`

#### ARM64 / aarch64

- release tests：**114 / 114**
- 最终 `target/release/solana-meme-arb opportunity-monitor` 直接运行
- Helius WSS subscriptions：32
- 同一真实 trigger：WSOL Mint，slot `439161856`
- 2 affected Meteora pools → 8 routes → **24 JSONL records**
- 20 evaluated + 4 insufficient
- `processed_updates=1`
- `reconnects=0`
- 峰值 RSS：**19,344 KB（约 18.9 MiB）**
- release smoke wall time：35.88 秒
- binary SHA256：`9e700e4c1efbd655385042462a093a3bdf2e1bc9ec73bcf316302197c950e16c`
- artifact ID：`9208425400`
- artifact size：`3,698,424` bytes
- artifact digest：`sha256:f904b5406545458abffaf1461f57273c100f5ed4693bf20764602dfc54e7769f`

这说明**生产 release 二进制本体**在两个 CPU 架构上都真实完成了 Helius/Mainnet 监控链路，而不是只通过编译/单测。

内存数据只是短时 1-update smoke 的峰值，不能冒充 72 小时内存上限；但它为 VPS 规格提供了第一份实际基准。

同一 commit 的普通 CI Run `31771794149` 也已全部成功。

## CI / 开发基础设施

- `actions/checkout@v7`
- `actions/cache@v5`
- Rust stable；当前实测 `rustc 1.97.1`
- `Cargo.lock` 已提交
- `fmt / check / clippy -D warnings / tests`
- V0–V3 真实联网回归
- V3.6.2 连续 WSS E2E
- x86_64 / ARM64 分离 release cache
- release artifact 必须通过 native release tests + live smoke + checksum
- README / deploy 文档更新不触发普通 Rust CI
- 同分支快速提交通过 `concurrency` 取消旧 CI

2026-08-14 曾出现 GitHub-hosted Runner 调度异常：Run `31769688476` 显示 `in_progress`，但 Job 长时间 0 steps。它不是 Rust monitor 超时；concurrency group 轮换为 `ci-v2-*` 后恢复正常。

## 已解决的重要问题

- Raydium API 真实字段 `mintA/mintB` 与旧 fixture 不一致：真实 CI 暴露后修正并加回归测试。
- Helius WSS Rustls CryptoProvider 缺失：显式启用 `ring`。
- 未知命令只打印 Usage 且退出 0：改为未知命令必须失败，避免 CI 假阳性。
- `-32016`：保持 `minContextSlot` 一致性并有限重试。
- Helius HTTP 429：短暂 HTTP 错误有界退避，不吞确定性错误。
- Orca/Meteora 不同 `Pubkey` crate：Context 层显式区分。
- `QuoteContext` large enum：三个 DEX variant 统一 Box，不关闭 Clippy。
- release artifact 首版 checksum 路径错误：真实下载检查时发现并修复。
- GitHub Runner 0-step 异常：通过 concurrency group 轮换脱离异常旧组，不修改业务逻辑。

## 当前边界 / 风险

- 只支持 6 个研究池；Raydium CLMM / Meteora DAMM v2 尚未接入。
- Orca Token-2022 transfer fee / Adaptive Fee 尚无对应真实池 E2E。
- 未初始化 Orca TickArray PDA 无法直接 WSS 订阅；后续首次初始化依赖动态依赖刷新纳入。
- 当前成本只是研究下界，不是实盘成本。
- 旧两小时样本中的正值受混合快照污染；修复后尚无新的正净利润证据。
- 已有两小时样本，**仍没有 24–72h 长期稳定性证据**。
- Release RSS 只有短时 smoke 数据，长期峰值仍需 VPS 样本确认。
- VPS 尚未部署，因此没有长时数据集。

## V3 完成标准

V3 只有在 Linux VPS 长时采样后才整体标记完成。至少回答：

1. Helius WSS 24–72 小时内断线/重连/订阅刷新频率。
2. 每个 Token / route / amount 的 evaluated 与 insufficient 比例。
3. gross-positive / net-positive 机会实际出现次数。
4. 正价差持续多少 slot / 多久。
5. 0.01 / 0.05 / 0.10 SOL 哪个规模更可执行。
6. 429 / `-32016` / stale / duplicate 的实际频率。
7. 实际 RSS / CPU / JSONL 增长是否稳定。
8. 数据是否支持继续 BONK/WIF、扩充 Universe，或根本不值得进入 V4。

## 下一步

### V3 长时真实采样

1. 选择 Linux VPS provider / region / CPU 架构。
2. 对目标 commit 运行 `release-build`，下载对应 x86_64 或 aarch64 artifact。
3. `sha256sum -c SHA256SUMS`。
4. 按 `deploy/README.md` 安装二进制、env 与 systemd。
5. 连续运行 24–72 小时。
6. 停止服务并取回完整 `opportunities.jsonl`。
7. 用 Python/pandas 做离线统计。
8. **只根据数据决定是否进入 V4。**

## 安全约定

- README、说明、代码注释使用中文；Rust 标识符使用英文。
- 每个有实际逻辑的函数设计对应测试；关键外部链路必须有真实 E2E。
- 绿色 CI 只是信号；关键 Run 必须核对实际日志。
- 只有实际运行/编译/Mainnet 数据/官方资料支持的结论才标记为已验证。
- 仓库不保存 Helius Key、钱包私钥、助记词或签名密钥。
- `.env` 保持在 `.gitignore`；只提交不含真实密钥的模板。
- V3 阶段禁止为了“跑起来”引入钱包或交易权限。

## 最近更新 — 2026-08-15

- Pool Universe 改为支持池型优先过滤 + 每 DEX TVL Top-N；每个样本文件绑定 `.universe` 清单，防止重启后静默混入不同池集合。
- WSS 改为触发信号；相关两腿的所有依赖与 Meteora Clock 使用同一批 RPC 快照。
- 正净利润候选至少跨到下一 slot 再做一次一致快照复核。
- 长时 JSONL 启动/退出校验改为流式扫描；仅恢复未写完的最后一行，并对每批追加执行数据同步。
- `.env.example` 与实际配置统一为 `HELIUS_API_KEY`。

### 2026-08-14

- V3.6.2 Run `31769969751`：114 tests，同一 WSS session 连续 2 update → 24 JSONL。
- 增加 provider-neutral Linux VPS 部署模板：systemd + env + 中文说明。
- `release-build` 升级为 x86_64 + ARM64 两个 native runner。
- 双架构 artifact 已实际下载并通过 checksum。
- Run `31771794143`：x86_64 / ARM64 **最终 release 二进制**都真实连接 Helius/Mainnet、处理 1 update 并写入 24 JSONL。
- 实测 release 峰值 RSS：x86_64 约 20.8 MiB；ARM64 约 18.9 MiB。
- 同 commit 普通 CI Run `31771794149` 全部成功。
- **当前部署前可验证的代码/CI/release 工作已收口；V3 下一步是实际 VPS 24–72 小时采样。**
