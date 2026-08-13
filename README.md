# solana-meme-arb

一个使用 Rust 开发的 Solana 成熟 Meme 跨 DEX 套利研究项目。

当前阶段以 **真实监控、真实验证** 为主。只有当监控数据证明机会具备可执行价值后，才会进入钱包、交易构造和实盘执行。

## 当前进度

**当前阶段：V2 — DEX Pool State 解析与本地 Swap Quote**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | 仓库、Rust 工程骨架、GitHub Actions CI | ✅ 已完成并验证 |
| V0 | BONK / WIF 跨 DEX 池发现与链上账户核验 | ✅ 已完成并验证 |
| V1 | Helius RPC/WSS 实时池账户订阅 | ✅ 已完成并验证 |
| V2 | DEX Pool State 解析与本地 Swap Quote | 🔄 当前进行 |
| V3 | 跨池套利机会计算、记录与统计 | ⏳ 未开始 |
| V4 | 原子交易构造与 `simulateTransaction` | ⏳ 未开始 |
| V5 | 小额实盘执行与 Jito 集成 | ⏳ 未开始 |

## 当前阶段成功标准

V2 只有满足以下条件才会标记完成：

1. 对当前实际监控的 Raydium、Orca、Meteora 池分别识别其链上状态结构。
2. 从 Helius `accountNotification` 的原始账户数据中解析出本地报价真正需要的字段。
3. 不依赖 DEX REST API 的价格字段完成至少一个池型的本地 Swap Quote。
4. 对每个解析/计算函数设计对应单元测试，并加入真实链上状态回归样本。
5. 对计算结果与官方 SDK / 官方报价来源做交叉核对；误差无法解释时不进入套利判断层。
6. 新增代码继续通过 `cargo fmt`、`cargo check`、`cargo clippy -D warnings`、`cargo test`。
7. V2 端到端真实数据验证通过后才进入 V3。

## 已验证结果

### Stage 0

GitHub Actions 已在真实 runner 上完成：

- `cargo fmt --all -- --check` ✅
- `cargo check --all-targets` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- `cargo test --all-targets` ✅
- CI 总结果：`success`
- 首次验证时实际 Rust 版本：`rustc 1.97.1`

### V0 — Pool Discovery

最终验收 CI：Run `31673566193`，结果 `success`。

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy -D warnings` ✅
- 单元测试：**23 passed / 0 failed** ✅
- 真实 DEX Pool Discovery ✅
- Solana Mainnet `getMultipleAccounts` 链上核验 ✅
- BONK：117 个精确交易对池被发现，筛选 9 个监控候选池。
- WIF：98 个精确交易对池被发现，筛选 9 个监控候选池。
- 18 个候选 Pool Account：**18/18 链上存在，18/18 owner 与预期 DEX Program 一致**。

### V1 — Helius 实时订阅

最终验收 CI：Run `31675468153`，结果 `success`。

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- 单元测试：**34 passed / 0 failed** ✅
- V0 真实 DEX Pool Discovery 回归测试 ✅
- V0 Solana Mainnet Pool Account owner 回归核验 ✅
- GitHub Actions Secret `HELIUS_API_KEY` 已确认可被工作流安全读取，日志中仅显示 `***`。
- Helius HTTP `getVersion` 真实通过：当次返回 `Solana core 4.2.0-rc.1`。
- Helius WSS 真实建立连接并对 **18 个候选池**发送 `accountSubscribe`。
- 18 个候选池的订阅确认全部完成后，真实收到 Pool Account 更新。
- 首次验收更新：`slot=438970104`，DEX=`Meteora DLMM`，Pool=`6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp`，`subscription=6580461`。
- 该通知已通过 `subscription id ↔ PoolInfo` 映射回正确 DEX 和 Pool Account。

因此 V1 的 HTTP、WSS、真实 Pool 订阅和通知映射链路均已有实际运行证据。

### V0 监控候选筛选规则

当前研究期默认：

- `MIN_MONITOR_TVL_USD = 1,000`
- `MAX_POOLS_PER_DEX = 3`

目的只是避免把免费 RPC/WSS 配额浪费在大量灰尘池上。**这两个值不是最终套利执行阈值，也不代表 TVL 低于 1,000 美元的池永远没有机会。**

### V0 真实候选池快照

以下 TVL 是 2026-08-13 验收时 API 返回的动态快照，只用于记录当时的筛选依据，后续会变化。

#### BONK / WSOL

| DEX | Pool Account | 类型 | TVL 快照 |
|---|---|---|---:|
| Orca | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` | Whirlpool | $120,951 |
| Orca | `BqnpCdDLPV2pFdAaLnVidmn3G93RP2p5oRdGEY2sJGez` | Whirlpool | $93,341 |
| Orca | `3ne4mWqdYuNiYrYZC9TrA3FcfuFdErghH97vNPbjicr1` | Whirlpool | $57,238 |
| Raydium | `GtKKKs3yaPdHbQd2aZS4SfWhy8zQ988BJGnKNndLxYsN` | Concentrated | $17,818 |
| Raydium | `HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv` | Standard | $13,018 |
| Meteora DLMM | `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp` | DLMM | $6,691 |
| Meteora DLMM | `7eexH14UjhNxJe6zTT3f1Vb1E8iACsBMVaWheDEmxdT2` | DLMM | $5,442 |
| Meteora DLMM | `3L4JX6RrssAHCxuzxosPBKuh6cHt6rXXbQU5hkeEpxku` | DLMM | $5,206 |
| Raydium | `ALYy1HRt3fsn1S9McLWsXCoq6Ke7bwMiwZ6QYr6HvsYS` | Concentrated | $3,399 |

#### WIF / WSOL

| DEX | Pool Account | 类型 | TVL 快照 |
|---|---|---|---:|
| Raydium | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` | Standard | $4,015,822 |
| Orca | `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1` | Whirlpool | $99,539 |
| Orca | `6qgyDW4fHvpTAmfNZvPAuETEbVwRKFVAuuHfNzvEmPkY` | Whirlpool | $13,970 |
| Raydium | `4mMDQ5kG9fFrBSQeedErsUoTBhY5KKnsKWGvenXRTwSy` | Concentrated | $4,797 |
| Meteora DLMM | `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V` | DLMM | $4,686 |
| Meteora DLMM | `DdqTmjucPjt2HXdzM24xp7HnGTorjpym1WnAJLSmLyhK` | DLMM | $3,883 |
| Orca | `A6cVoMU1Z7oRi9R774QarVxHUmHPQkxQfYVFGNQuAx2b` | Whirlpool | $2,911 |
| Raydium | `BuavWdfsNTfmEQbnPt2PLc51B7pifRNhqNiDUtGLeNNn` | Concentrated | $1,490 |
| Meteora DLMM | `39deGQ4Ucue7tGXRDRHvRLz8zZQ6tu7QFkKq5UYiDQ9N` | DLMM | $1,254 |

本次筛选中 Meteora DAMM v2 没有 BONK/WSOL 或 WIF/WSOL 池达到当前 `$1,000` TVL 门槛，因此没有强行加入监控列表。

## 已遇到并解决的问题

### Raydium API 字段与旧样本不一致

第一次真实联网测试失败：解析器期待 `mint1 / mint2`，但当前 Raydium v3 Pool 响应实际使用 `mintA / mintB`。

处理方式：

1. 保留真实 CI 错误。
2. 核对 Raydium 官方 SDK 当前类型定义。
3. 将解析器改为 `mintA / mintB`。
4. 增加回归测试：当前结构必须通过，旧 `mint1 / mint2` Pool 结构必须被拒绝。
5. 重新跑真实 API 测试并通过。

### 大量灰尘池

真实 discovery 会返回大量 TVL 极低的池。直接全部进入 WSS 订阅会浪费配额，也降低后续状态管理质量。

处理方式：增加可单独测试的 `select_monitoring_candidates`，当前按 TVL 下限和每 DEX 数量上限筛选。该函数已覆盖低 TVL、每 DEX 限额、零限额等测试。

### API 返回地址与链上真实性之间的边界

最初只能确认“DEX API 返回了这个地址”，还不能称为“链上已核验”。V0 后续新增 Solana `getMultipleAccounts` 校验：候选账户必须存在，并且账户 `owner` 必须等于预期 DEX Program，否则测试失败。

最终 18 个候选池全部通过。

### V1 `clippy` 阻止不必要的 `expect`

V1 首次完整编译后，`clippy -D warnings` 拒绝了“先 `is_some()` 再 `expect()`”的取值方式。没有屏蔽 lint，而是改成 `if let Some(update)` 模式匹配。修复后 `clippy` 与全部测试通过。

### V1 Helius WSS 首次真实连接触发 Rustls CryptoProvider panic

第一次注入真实 Helius Secret 后，HTTP 已成功，但 WSS 在 TLS 初始化时 panic：`rustls 0.23.43` 无法自动确定进程级 `CryptoProvider`。

根因：`tokio-tungstenite` 的 Rustls 依赖关闭了默认特性，当前依赖组合没有提供唯一的 TLS 加密后端。

处理方式：

1. 保留失败 CI：Run `31675055836`。
2. 核对 `rustls 0.23.43` 官方特性定义。
3. 在 `Cargo.toml` 显式启用 `rustls` 的 `ring` CryptoProvider。
4. 不绕过 panic、不关闭 TLS 校验。
5. 重新执行全部格式、编译、Clippy、测试、真实 Pool Discovery、链上 owner 校验和 Helius HTTP/WSS 测试。
6. 修复后 Run `31675468153` 全部成功，并真实收到 Pool Account 更新。

## 当前问题 / 阻塞 / 风险

- 当前开发主要依赖 GitHub Actions 进行 Rust 编译和真实联网测试，因为本次 ChatGPT 执行环境没有可直接使用的 Rust toolchain。
- `actions/checkout@v4` 当前在 GitHub runner 上有 Node.js 20 弃用警告；GitHub 当前强制使用 Node 24，本项目 CI 仍成功。后续单独处理，不与套利业务逻辑混改。
- `HELIUS_API_KEY` 没有写入仓库或 Git 历史，仅由 GitHub Actions Secret 在运行时注入。
- DEX REST API 的 TVL 属于动态外部数据，不能视为链上实时成交价格。
- 当前已经证明实时订阅链路有效；**尚未证明我们能正确解析各池状态、正确本地报价或策略可盈利**。
- 当前 GitHub App 无权读取个人 Billing Usage API，因此 README 不记录猜测的 Actions 剩余额度；需要从 GitHub `Settings → Billing & Licensing` 查看真实账户用量。

## 下一步

V2 当前计划：

1. 先梳理当前 18 个候选池分别属于哪些真实池型。
2. 从最简单且流动性较深的池型开始解析真实账户数据。
3. 为账户布局解析函数加入固定链上样本测试。
4. 实现本地 Swap Quote，并与官方报价交叉核对。
5. 一个池型完整通过后再扩展到下一个池型，避免同时写三套未验证逻辑。

## 开发与记录约定

- README、项目说明和代码注释可以使用中文。
- Rust 的变量名、函数名、结构体名、模块名保持英文。
- 注释重点解释“为什么这样做”“链上字段代表什么”“风险在哪里”，避免给显而易见的代码逐行加注释。
- 每个有实际逻辑的函数都设计对应测试；依赖真实外部服务的函数再增加端到端真实测试。
- 每完成一个阶段，README 更新阶段状态、验证结果和下一步。
- 遇到会影响推进的真实问题时，立即写入“当前问题 / 阻塞 / 风险”，解决后保留必要的解决记录。
- 只有实际运行、实际编译、实际链上数据或官方资料能够支持的结论才标记为已验证。

## 安全规则

仓库不会保存：

- Helius API Key
- 钱包私钥
- 助记词
- 任何其他交易签名密钥

本地 `.env` 文件必须保持在 `.gitignore` 中；仓库只保留不含真实密钥的 `.env.example`。

## 最近更新

**2026-08-13**

- Stage 0 完成并验证。
- V0 Pool Discovery 完成并验证。
- BONK/WIF 共 18 个监控候选 Pool Account 完成链上存在性与 owner 核验。
- V1 Helius 配置、HTTP/WSS 协议层与订阅映射逻辑完成。
- V1 单元测试：**34 passed / 0 failed**。
- 第一次真实 Helius WSS 测试暴露 Rustls CryptoProvider 配置问题，失败记录保留。
- 显式启用 `ring` CryptoProvider 后重新验收。
- V1 最终 CI Run `31675468153`：全部步骤成功。
- Helius HTTP 和 WSS 均真实通过，18 个候选池已订阅并收到真实 Meteora DLMM Pool Account 更新。
- V1 正式完成，进入 V2 Pool State 解析与本地 Swap Quote。
