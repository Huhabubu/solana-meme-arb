# solana-meme-arb

一个使用 Rust 开发的 Solana 跨 DEX 套利研究项目。

> 当前原则：**真实监控、真实验证、先研究后交易。** 只有长时间监控数据证明机会具有可执行价值后，才进入钱包、交易构造和实盘执行。

## 当前状态

**当前阶段：V3 — 代码层已具备常驻监控条件，等待 Linux VPS 24–72 小时真实采样。**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | Rust 工程、私有仓库、GitHub Actions CI | ✅ |
| V0 | BONK/WIF 跨 DEX Pool Discovery + 链上 owner 核验 | ✅ |
| V1 | Helius HTTP/WSS 实时账户订阅 | ✅ |
| V2 | 三 DEX Pool State + 本地 exact-input Quote + 依赖账户触发 | ✅ |
| V3 | 跨池闭环、金额曲线、成本、实时重算、JSONL、常驻 monitor | 🔄 代码完成；等待 24–72h 长时样本 |
| V4 | 原子交易构造 + `simulateTransaction` | ⏳ |
| V5 | 小额实盘 + Jito | ⏳ |

### V3 子模块

| 子模块 | 状态 | 已验证内容 |
|---|---|---|
| 统一 `SwapQuote` / 两腿闭环 | ✅ | Mint/amount 连续性、同池拒绝、signed profit、slot 范围 |
| Raydium Standard 双方向 Quote | ✅ | `WSOL ↔ Token` 主网实测 |
| Orca Whirlpool 双方向 Quote | ✅ | 官方 core Quote + TickArray 主网实测 |
| Meteora DLMM 双方向 Quote | ✅ | 官方 Rust Quote + BinArray 主网实测 |
| 三 DEX 全有向路径 | ✅ | 每 Token 3 池 → 6 路；BONK/WIF 共 12 路 |
| 多金额利润曲线 | ✅ | 0.01 / 0.05 / 0.10 SOL，同一腿快照本地批量 Quote |
| 流动性不足 | ✅ | 显式 `insufficient_liquidity`，不会伪装成亏损/成功 |
| 执行成本 / 净利润 | ✅ | `ExecutionCost → NetOpportunity` |
| RPC 恢复 | ✅ | `-32016` 有限重试；429/408/部分 5xx 有界退避，不降低 `minContextSlot` |
| affected-route 实时路由 | ✅ | WSS update → affected pool → related routes |
| `QuoteContextCache` | ✅ | 只刷新 affected Context，相关路径两腿本地 Quote |
| JSONL 持久化 / 统计 | ✅ | append-only、严格重读、分组统计 |
| 连续 monitor | ✅ | 同一 WSS session 连续处理真实 update、去重/stale、重连、依赖刷新 |
| Linux VPS 部署准备 | ✅ | systemd/env 模板、release artifact、checksum 流程均已验证 |
| 24–72h 长时采样 | 🔄 | **尚未实际执行，因此 V3 仍未整体完成** |

## 当前研究 Universe

BONK/WIF 只是两个并行测试样本，套利引擎本身不与这两个 Token 绑定。后续会根据长时数据扩展为动态 Universe。

当前完整支持 6 个研究池：

| Token | DEX | Pool |
|---|---|---|
| BONK | Raydium Standard | `HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv` |
| BONK | Orca Whirlpool | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` |
| BONK | Meteora DLMM | `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp` |
| WIF | Raydium Standard | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` |
| WIF | Orca Whirlpool | `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1` |
| WIF | Meteora DLMM | `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V` |

当前尚未接入本地 Quote Engine：

- Raydium CLMM
- Meteora DAMM v2
- 其他 Solana DEX / Pool 类型

## 当前套利链路

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
只生成 affected Token 的 related directed routes
        ↓
0.01 / 0.05 / 0.10 SOL 本地闭环 Quote
        ↓
Gross Profit
        ↓
ExecutionCost
        ↓
NetOpportunity
        ↓
OpportunityRecord
        ↓
append-only JSONL
        ↓
增量统计
        ↓
继续 next_update()
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
| V3.5.2 | `31765141544` | 104 | affected Context 局部刷新，route evaluator 不再拉 Pool snapshot |
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

两条真实更新都发生在同一个 `session=1`，CI 额外强制检查：

```text
processed_updates=2
max_updates_in_single_session=2
```

因此“两次重连、每次只处理一条”不能冒充该验收成功。

> 两条 update 只证明常驻循环可工作，**不能代替长期稳定性和机会分布样本**。

## 成本模型边界

当前 V3 使用一个仅用于研究链路验证的 **6,000 lamports 成本下界**：

```text
Base fee       5,000
Priority fee       0
Jito tip        1,000
Other               0
────────────────────
Total            6,000 lamports
```

DEX swap fee 已体现在两腿 Quote 输出中，不会重复扣除。

> 6,000 lamports **不是未来实盘 landing cost**。V4 构造出真实交易后，Priority Fee 与 Jito Tip 必须根据实际 CU 和竞争状态动态估计。

目前所有已观察 V3 快照均未提供正净利润证据。

## Linux VPS 部署准备

代码层现在已经具备 24–72 小时常驻采样条件，但 **VPS 尚未购买/部署，长时采样尚未开始**。

仓库已增加：

```text
deploy/
├── README.md
├── monitor.env.example
└── solana-meme-arb.service

.github/workflows/
├── ci.yml
└── release-build.yml
```

### systemd

`deploy/solana-meme-arb.service`：

- 专用无登录用户 `solana-arb`；
- `Restart=on-failure`；
- Helius Key 从 `/etc/solana-meme-arb/monitor.env` 读取；
- JSONL 写入 `/var/lib/solana-meme-arb/`；
- `ProtectSystem=strict`，只开放研究数据目录写权限；
- 当前不配置钱包或任何签名材料。

详细命令见 `deploy/README.md`。

## Release 构建链路

生产采样不在 VPS 上现编 Rust 依赖。使用 GitHub Actions `release-build`：

```text
cargo build --release --locked
        ↓
cargo test --release --locked --all-targets
        ↓
生成 deployment bundle
        ↓
SHA256SUMS
        ↓
Actions artifact
```

### 已验证 release

最终验证 Run：`31770886583`

- `actions/checkout@v7` ✅
- release Cargo cache **primary key hit** ✅
- cache-hit `cargo build --release --locked`：**19.44 秒**
- release tests：**114 passed / 0 failed**
- Linux x86_64 binary：约 **9.4 MB**
- binary SHA256：
  `21c35aa1bf1f05e19809a44440bd783595fa6119aac3418f9e523daf358c805e`
- artifact ID：`9208092741`
- artifact size：`3,739,107` bytes
- artifact digest：
  `sha256:676cd216f327f1608196410d94e9762443c908620e83f8c11756cb3e79c422e9`

实际把 artifact 下载并解压后执行：

```bash
sha256sum -c SHA256SUMS
```

已得到：

```text
solana-meme-arb: OK
```

因此当前已经真实验证：

```text
GitHub commit
   ↓
locked release build
   ↓
114 release tests
   ↓
deployment bundle
   ↓
Actions artifact
   ↓
下载 / 解压
   ↓
SHA256 校验通过
```

第一次冷 release build 曾耗时约 **7分12秒**；release cache 建成后下降到 **19.44 秒**。因此 release workflow 不放进每次普通开发提交，只在部署/发布时使用。

## CI / 开发基础设施

当前普通 CI：

- `actions/checkout@v7`
- `actions/cache@v5`
- Rust stable；当前实测 `rustc 1.97.1`
- `fmt`
- `cargo check --all-targets`
- `clippy -D warnings`
- `cargo test --all-targets`
- V0/V1/V2/V3 真实联网回归
- V3.6.2 连续 WSS E2E

最新普通 CI Run `31770886685`：**全部成功**。

缓存：

- 普通 Cargo cache 约 958 MB；
- release cache 约 1.29 GB；
- key 绑定 OS + Rust 版本 + `Cargo.toml` + `Cargo.lock`；
- `Cargo.lock` 已提交；
- README / deploy 文档更新不触发普通 Rust CI；
- 同一分支快速连续提交通过 `concurrency` 取消旧 CI。

2026-08-14 曾出现一次 GitHub-hosted Runner 调度异常：Run `31769688476` 显示 `in_progress`，但 Job 长时间 0 steps、`updated_at` 停止。该问题不是 Rust monitor 超时。CI concurrency group 轮换到 `ci-v2-*` 后，新 Run 正常执行。

## 已解决的重要问题

- Raydium API 从旧 fixture 的 `mint1/mint2` 变化到真实 `mintA/mintB`：已修并加回归测试。
- Helius WSS 首次真实连接因 Rustls CryptoProvider 缺失 panic：显式启用 `ring` 后端。
- 未知命令打印 Usage 但退出码 0 导致 CI 假阳性：命令解析已改为未知命令必须失败。
- `-32016 Minimum context slot has not been reached`：保持一致性要求并有限重试，不读取更旧状态。
- Helius HTTP 429：只对短暂 HTTP 错误做有界退避，不吞确定性错误。
- Orca/Meteora 不同 Solana `Pubkey` crate 版本：Context 层显式区分。
- `QuoteContext` large enum：三个 DEX variant 统一 Box，Clippy 不关闭。
- release artifact 首版 `SHA256SUMS` 带 `dist/` 前缀：真实下载检查时发现，已修；第二版可直接 `sha256sum -c`。

## 当前风险 / 未完成边界

- 只支持 6 个研究池；Raydium CLMM / Meteora DAMM v2 尚未接入。
- Orca Token-2022 transfer fee / Adaptive Fee 尚无对应真实池 E2E 样本。
- 未初始化 Orca TickArray PDA 本地 Quote 可按空数组语义处理，但不存在的账户无法直接 WSS 订阅；后续首次初始化依赖其他已订阅更新触发动态依赖刷新。
- 当前成本只是研究下界，不是实盘成本。
- 目前没有正净利润证据。
- V3.6.2 只验证两条连续 update，**没有长期稳定性证据**。
- VPS 尚未部署，因此还没有 24–72 小时数据集。

## V3 完成标准

V3 只有在 Linux VPS 长时采样后才整体标记完成。至少要回答：

1. Helius WSS 24–72 小时内断线多少次？重连是否稳定？
2. 每个 Token / DEX route / amount 的 evaluated 与 insufficient 比例是多少？
3. gross-positive / net-positive 机会到底出现多少次？
4. 正价差能持续多少 slot / 多久？
5. 0.01 / 0.05 / 0.10 SOL 哪个规模在真实池深度下更可执行？
6. 429 / `-32016` / stale / duplicate / subscription refresh 的实际频率是多少？
7. 数据是否支持继续 BONK/WIF、扩展动态 Universe，还是根本不值得进入 V4？

## 下一步

### V3 长时真实采样

1. 选择 Linux VPS provider / region / 规格。
2. 针对最终部署 commit 手动运行 `release-build`。
3. 下载 artifact 并 `sha256sum -c SHA256SUMS`。
4. 按 `deploy/README.md` 安装二进制、env 与 systemd。
5. 连续运行 24–72 小时。
6. 停止服务，取回完整 `opportunities.jsonl`。
7. 用 Python/pandas 做离线统计。
8. **只根据数据决定是否进入 V4。**

## 安全约定

- README、说明、代码注释使用中文；Rust 标识符使用英文。
- 每个有实际逻辑的函数设计对应测试；关键外部链路必须有真实 E2E。
- 绿色 CI 只是信号，关键 Run 必须读取实际日志。
- 只有实际运行/编译/主网数据/官方资料支持的结论才标记为已验证。
- 仓库不保存 Helius Key、钱包私钥、助记词或签名密钥。
- `.env` 保持在 `.gitignore`；只提交不含真实密钥的模板。
- V3 阶段禁止为了“跑起来”引入钱包或交易权限。

## 最近更新 — 2026-08-14

- V3.6.2 Run `31769969751`：114 tests，同一 WSS session 连续 2 update → 24 JSONL。
- 清理所有 V3.6.2 one-shot workflow，只保留正式 CI / release workflow。
- 普通 CI 升级到 `actions/checkout@v7`，最新 Run `31770886685` 全部成功。
- 增加 provider-neutral Linux VPS 部署模板：systemd + env + 中文部署文档。
- 增加永久 `release-build` workflow；使用 `--release --locked`、release tests、checksum 和 artifact。
- Release Run `31770886583`：release cache 命中，19.44 秒完成 build，114/114 release tests。
- 下载 artifact 后实际执行 `sha256sum -c SHA256SUMS`：`solana-meme-arb: OK`。
- **当前已 deployment-ready；V3 下一步是实际 VPS 24–72 小时采样，尚未开始。**
