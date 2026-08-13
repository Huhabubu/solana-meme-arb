# solana-meme-arb

一个使用 Rust 开发的 Solana 成熟 Meme 跨 DEX 套利研究项目。

当前阶段以 **真实监控、真实验证** 为主。只有当监控数据证明机会具备可执行价值后，才会进入钱包、交易构造和实盘执行。

## 当前进度

**当前阶段：V1 — Helius RPC/WSS 实时池账户订阅**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | 仓库、Rust 工程骨架、GitHub Actions CI | ✅ 已完成并验证 |
| V0 | BONK / WIF 跨 DEX 池发现与链上账户核验 | ✅ 已完成并验证 |
| V1 | Helius RPC/WSS 实时池账户订阅 | 🔄 当前进行 |
| V2 | DEX Pool State 解析与本地 Swap Quote | ⏳ 未开始 |
| V3 | 跨池套利机会计算、记录与统计 | ⏳ 未开始 |
| V4 | 原子交易构造与 `simulateTransaction` | ⏳ 未开始 |
| V5 | 小额实盘执行与 Jito 集成 | ⏳ 未开始 |

## 当前阶段成功标准

V1 只有满足以下条件才会标记完成：

1. Helius HTTP RPC 可真实连接并完成基础请求。
2. Helius WSS（WebSocket Secure，安全 WebSocket 连接）可真实建立连接。
3. 对 V0 选出的真实 Pool Account 发起 `accountSubscribe`。
4. 至少真实收到池账户更新通知，并能识别通知属于哪个池、哪个 DEX。
5. 断线、无效订阅响应等基础错误不能静默吞掉。
6. 新增逻辑有对应测试，并通过 `cargo fmt`、`cargo check`、`cargo clippy`、`cargo test`。
7. V1 端到端真实连接测试通过后才标记完成。

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

### V0 监控候选筛选规则

当前研究期默认：

- `MIN_MONITOR_TVL_USD = 1,000`
- `MAX_POOLS_PER_DEX = 3`

目的只是避免在 V1 阶段把免费 RPC/WSS 配额浪费在大量灰尘池上。**这两个值不是最终套利执行阈值，也不代表 TVL 低于 1,000 美元的池永远没有机会。**

### V0 真实候选池快照

以下 TVL 是 2026-08-13 最终验收时 API 返回的动态快照，只用于记录当时的筛选依据，后续会变化。

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

## 当前问题 / 阻塞 / 风险

- 当前开发主要依赖 GitHub Actions 进行 Rust 编译和真实联网测试，因为本次 ChatGPT 执行环境没有可直接使用的 Rust toolchain。
- `actions/checkout@v4` 当前在 GitHub runner 上有 Node.js 20 弃用警告；GitHub 当前强制使用 Node 24，本项目 CI 仍成功。后续单独处理，不与套利业务逻辑混改。
- 用户提供的临时 Helius API Key **尚未写入仓库，也尚未提交到 Git 历史**。V1 需要安全注入后才使用。
- DEX REST API 的 TVL 属于动态外部数据，不能视为链上实时成交价格。
- 当前只证明“池发现与账户身份核验链路有效”，**没有任何证据证明套利策略可盈利**。

## 下一步

正在推进 V1：

1. 建立 Helius 配置读取层，只允许从环境变量读取 Key / RPC / WSS 地址。
2. 先测试 Helius HTTP RPC 连通性。
3. 建立 WSS 连接和 `accountSubscribe` 请求模型。
4. 订阅 V0 候选池，并记录 `subscription id ↔ PoolInfo` 映射。
5. 真实等待 Pool Account 更新，确认能够识别更新属于哪个池。
6. 加入最小必要的连接错误、JSON 错误和订阅响应测试。

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
- V0 最终 CI：23 个单元测试全部通过。
- BONK/WIF 共 18 个监控候选 Pool Account 完成链上存在性与 owner 核验。
- 修复 Raydium v3 `mintA / mintB` 字段兼容问题。
- 增加灰尘池筛选和链上账户核验。
- 开始 V1 Helius RPC/WSS 实时订阅。
