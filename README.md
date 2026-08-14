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
| 三 DEX 全有向路径 | ✅ | 每 Token 3 池 → 6 路；BONK/WIF 共 12 路 |
| 多金额利润曲线 | ✅ | 0.01 / 0.05 / 0.10 SOL，同一快照本地批量 Quote |
| 流动性不足 | ✅ | 显式 `insufficient_liquidity` |
| 执行成本 / 净利润 | ✅ | `ExecutionCost → NetOpportunity` |
| RPC 恢复 | ✅ | `-32016` 有限重试；429/408/部分 5xx 有界退避 |
| affected-route 实时路由 | ✅ | WSS update → affected pool → related routes |
| `QuoteContextCache` | ✅ | 只刷新 affected Context；相关路径完全本地 Quote |
| JSONL 持久化 / 统计 | ✅ | append-only、严格重读、分组统计 |
| 连续 monitor | ✅ | 长连接、去重/stale、有限重连、动态依赖刷新 |
| Linux VPS 部署准备 | ✅ | systemd/env、x86_64/ARM64 release artifact、checksum |
| 24–72h 长时采样 | 🔄 | **尚未实际执行，因此 V3 尚未整体完成** |

## 当前研究 Universe

BONK/WIF 只是两个并行测试样本，套利引擎不与这两个 Token 绑定。后续根据长时数据决定是否扩展为动态 Universe。

当前完整支持 6 个研究池：

| Token | DEX | Pool |
|---|---|---|
| BONK | Raydium Standard | `HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv` |
| BONK | Orca Whirlpool | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` |
| BONK | Meteora DLMM | `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp` |
| WIF | Raydium Standard | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` |
| WIF | Orca Whirlpool | `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1` |
| WIF | Meteora DLMM | `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V` |

尚未进入本地 Quote Engine：Raydium CLMM、Meteora DAMM v2 以及其他 Solana DEX/Pool 类型。

## 当前实时链路

```text
Helius WSS long-lived session
        ↓
RawAccountUpdate
        ↓
duplicate / stale slot 过滤
        ↓
QuoteState
        ↓
只刷新 affected Pool QuoteContext
        ↓
related directed routes
        ↓
0.01 / 0.05 / 0.10 SOL 本地闭环 Quote
        ↓
Gross Profit → ExecutionCost → NetOpportunity
        ↓
OpportunityRecord
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

目前所有已观察 V3 快照均未提供正净利润证据。

## Linux VPS 部署准备

仓库已经提供 provider-neutral 部署层：

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

## Release 构建链路

生产采样不在 VPS 上现编 Rust 依赖。`release-build` 使用原生 GitHub-hosted runner 分别生成：

```text
Linux x86_64 artifact
Linux ARM64/aarch64 artifact
```

每个架构均执行：

```text
cargo build --release --locked
        ↓
cargo test --release --locked --all-targets
        ↓
file 架构断言
        ↓
SHA256SUMS
        ↓
Actions artifact
```

### 双架构最终验收

Run `31771245684`，commit `48e3e51c14996a3374c9f824f9220d63e1380057`：

#### x86_64

- native runner：`ubuntu-24.04`
- release build ✅
- release tests：**114 passed / 0 failed**
- `file`：ELF 64-bit x86-64 ✅
- artifact ID：`9208224334`
- artifact size：`3,740,250` bytes
- artifact digest：`sha256:5fdd56ce15744b16e45827bf03d4a392308ba57839163f1edb73e7f6f90f4740`

#### ARM64 / aarch64

- native runner：`ubuntu-24.04-arm`
- 首次冷 `cargo build --release --locked`：**5分31秒**
- release tests：**114 passed / 0 failed**
- binary：约 **8.9 MB**
- `file`：`ELF 64-bit ... ARM aarch64` ✅
- binary SHA256：`9e700e4c1efbd655385042462a093a3bdf2e1bc9ec73bcf316302197c950e16c`
- artifact ID：`9208302819`
- artifact size：`3,698,494` bytes
- artifact digest：`sha256:457b01d89cf45c76378211ab3faf35a7e10be619273db92d4580b35307413f38`
- ARM release cache 已保存供后续复用。

两个 artifact 都实际下载、解压并执行：

```bash
sha256sum -c SHA256SUMS
```

均得到：

```text
solana-meme-arb: OK
```

因此当前部署不再被 CPU 架构锁死：**x86_64 与 ARM64 Linux VPS 都有经过原生 release build、114 个 release tests、架构断言和 checksum 的可部署产物。**

同一 commit 的普通 CI Run `31771245661` 也已全部成功。

## CI / 开发基础设施

- `actions/checkout@v7`
- `actions/cache@v5`
- Rust stable；当前实测 `rustc 1.97.1`
- `Cargo.lock` 已提交
- `fmt / check / clippy -D warnings / tests`
- V0–V3 真实联网回归
- V3.6.2 连续 WSS E2E
- 普通 Cargo cache 与不同架构的 release cache 分离
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

## 当前边界 / 风险

- 只支持 6 个研究池；Raydium CLMM / Meteora DAMM v2 尚未接入。
- Orca Token-2022 transfer fee / Adaptive Fee 尚无对应真实池 E2E。
- 未初始化 Orca TickArray PDA 无法直接 WSS 订阅；后续首次初始化依赖动态依赖刷新纳入。
- 当前成本只是研究下界，不是实盘成本。
- 目前没有正净利润证据。
- V3.6.2 只有两条连续 update，**没有 24–72h 长期稳定性证据**。
- VPS 尚未部署，因此没有长时数据集。

## V3 完成标准

V3 只有在 Linux VPS 长时采样后才整体标记完成。至少回答：

1. Helius WSS 24–72 小时内断线/重连/订阅刷新频率。
2. 每个 Token / route / amount 的 evaluated 与 insufficient 比例。
3. gross-positive / net-positive 机会实际出现次数。
4. 正价差持续多少 slot / 多久。
5. 0.01 / 0.05 / 0.10 SOL 哪个规模更可执行。
6. 429 / `-32016` / stale / duplicate 的实际频率。
7. 数据是否支持继续 BONK/WIF、扩充 Universe，或根本不值得进入 V4。

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

## 最近更新 — 2026-08-14

- V3.6.2 Run `31769969751`：114 tests，同一 WSS session 连续 2 update → 24 JSONL。
- 增加 provider-neutral Linux VPS 部署模板：systemd + env + 中文说明。
- 普通 CI 升级 `actions/checkout@v7`，同 commit 最新 Run `31771245661` 成功。
- `release-build` 升级为 x86_64 + ARM64 两个原生 runner 的矩阵构建。
- 双架构 Run `31771245684`：两边 release build、114/114 release tests、架构断言、artifact 均成功。
- x86_64 与 ARM64 artifact 都实际下载并通过 `sha256sum -c SHA256SUMS`。
- **当前已完成部署前全部代码/CI/release 准备；V3 下一步是实际 VPS 24–72 小时采样。**
