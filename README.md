# solana-meme-arb

一个使用 Rust 开发的 Solana 成熟 Meme 跨 DEX 套利研究项目。

当前阶段以 **真实监控、真实验证** 为主。只有当监控数据证明机会具备可执行价值后，才会进入钱包、交易构造和实盘执行。

## 当前进度

**当前阶段：V3 — 跨池套利机会计算、记录与统计**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | 仓库、Rust 工程骨架、GitHub Actions CI | ✅ 已完成并验证 |
| V0 | BONK / WIF 跨 DEX 池发现与链上账户核验 | ✅ 已完成并验证 |
| V1 | Helius RPC/WSS 实时池账户订阅 | ✅ 已完成并验证 |
| V2 | DEX Pool State 解析、本地 Swap Quote、依赖账户实时触发 | ✅ 已完成并验证 |
| V3 | 跨池套利机会计算、记录与统计 | 🔄 当前进行 |
| V4 | 原子交易构造与 `simulateTransaction` | ⏳ 未开始 |
| V5 | 小额实盘执行与 Jito 集成 | ⏳ 未开始 |

### V2 最终子模块状态

| 子模块 | 状态 | 最终证据 |
|---|---|---|
| Raydium Standard AMM v4 | ✅ 已完成并真实验证 | Pool State + 两个 vault + 本地 exact-input Quote |
| Orca Whirlpool | ✅ 已完成并真实验证 | Whirlpool + TickArray + Mint + 可选 Oracle + 官方 core Quote |
| Meteora DLMM | ✅ 已完成并真实验证 | LbPair + BinArray + Mint + 可选 bitmap extension + 官方 Rust Quote |
| 实时报价依赖账户状态管理 | ✅ 已完成并真实验证 | 31 个依赖账户订阅；真实 TickArray 更新映射到正确池并触发 Quote 重算 |

> 当前“可报价池集合”是每个跟踪 Token 各选 1 个 Raydium Standard、1 个 Orca Whirlpool、1 个 Meteora DLMM，共 **6 个池**。V0 的 18 个候选池仍用于发现/核验，但 Raydium CLMM 和 Meteora DAMM v2 尚未进入本地 Quote 引擎，因此 V2 没有声称“18 个池全部可报价”。

## 当前阶段成功标准

V3 只有满足以下条件才会标记完成：

1. 对同一 Token 的不同可报价池，以同一输入资产和输入金额计算可比较的真实本地 Quote。
2. 建立两腿闭环，例如 `WSOL → BONK → WSOL`，不能只比较表面价格。
3. 明确区分毛利润和净利润；净利润至少扣除两腿 DEX fee，并为后续 Priority Fee / Jito Tip / 交易成本预留独立成本项。
4. 对多个输入金额计算闭环结果，避免把某一个固定金额的价差误当成可执行机会。
5. 实时依赖账户变化后，只重算受影响池，并更新对应 Token 的跨池机会。
6. 机会记录至少包含时间、slot、Token、买入池、卖出池、输入金额、两腿输出、毛利润/收益率和可解释的成本字段。
7. 每个新增计算函数有单元测试；真实联网层用 Helius + 主网池做端到端验证。
8. 连续监控数据真实证明系统能够稳定发现/记录机会后，才讨论 V4 交易构造。

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

### V2 — Raydium Standard AMM v4

Raydium 子模块已通过真实链上验收。

实现与验证内容：

- 依据 Raydium 当前 AMM v4 `AmmInfo` packed 布局解析 **752 字节 Pool State**。
- 真实读取 `coin_vault`、`pc_vault` 两个经典 SPL Token Account，并校验 owner / mint。
- 有效储备量先扣除 `need_take_pnl`。
- Swap fee 使用与程序一致的向上取整规则。
- exact-input 输出按恒定乘积整数公式向下取整。
- Pool State 与 vault 读取使用 `minContextSlot` 防止明显倒退到更旧状态。
- BONK / WIF 两个已选 Raydium Standard 池均真实报价成功。

V2 最终验收 Run `31687579494` 的真实快照：

| Pair | Pool | 输入 | 本地输出 | Fee |
|---|---|---:|---:|---:|
| BONK/WSOL | `HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv` | 0.01 WSOL | 332,459.25755 BONK | 25 / 10,000 |
| WIF/WSOL | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` | 0.01 WSOL | 5.472466 WIF | 25 / 10,000 |

### V2 — Orca Whirlpool

Orca 子模块已通过真实链上验收。

实现与验证内容：

- 使用 Orca 官方 `orca_whirlpools_client 8.0.0` 解码 Whirlpool / TickArray / Oracle。
- 使用 Orca 官方 `orca_whirlpools_core 2.1.1` 的 Quote 引擎计算 exact-input Quote，没有自行重写 CLMM 数学。
- 根据当前 tick 与 tick spacing 推导报价依赖 TickArray。
- 使用同一 RPC `context.slot` 构造 Whirlpool + TickArray + Mint + 可选 Oracle 的报价快照。
- 若读取过程中依赖集合发生变化，则拒绝拼接状态并要求重试。
- 未初始化 TickArray 按 Orca 官方 SDK 的空数组语义处理。
- BONK / WIF 当前选中的 Whirlpool 均为普通费率池，`adaptive_fee=false`。

V2 最终验收 Run `31687579494` 的真实快照：

| Pair | Pool | 输入 | 本地输出 | tick spacing | fee rate |
|---|---|---:|---:|---:|---:|
| BONK/WSOL | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` | 0.01 WSOL | 332,306.41516 BONK | 8 | 500 |
| WIF/WSOL | `D6NdKrKNQPmRZCCnG1GqXtF7MMoHB7qR6GU5TkG59Qz1` | 0.01 WSOL | 5.472831 WIF | 4 | 400 |

> Adaptive Fee 的 Oracle 分支已有布局/逻辑单元测试，但当前 BONK/WIF 真实可报价池没有触发该分支，因此仍不声称“Adaptive Fee 已完成真实链上实池验证”。

### V2 — Meteora DLMM

Meteora DLMM 子模块已在最终验收 Run `31687579494` 中真实通过。

实现与验证内容：

- 使用 Meteora 官方 Rust `commons` SDK，固定到 git revision `fb02e51ae677bbd18e76543f702dae40632426db`。
- 校验 Anchor discriminator 后解析 `LbPair`、`BinArrayBitmapExtension`、`BinArray`。
- 根据输入 Mint 判断 `X→Y / Y→X` 方向。
- 通过官方 helper 推导当前方向真正需要的 BinArray。
- Clock sysvar 不加入 WSS 依赖订阅；Clock 每个 slot 都变化，若订阅会让所有 DLMM 池无意义地每 slot 触发。真正的 DLMM 依赖变化后再刷新 Clock。
- 最终 Quote 调用 Meteora 官方 `quote_exact_in`，没有自行重写 DLMM 数学。

最终真实快照：

| Pair | Pool | 输入 | 本地输出 | active_id | BinArray | fee / protocol fee |
|---|---|---:|---:|---:|---:|---:|
| BONK/WSOL | `6oFWm7KPLfxnwMb3z5xwBoXNSPP3JJyirAPqPSiVcnsp` | 0.01 WSOL | 332,438.23480 BONK | -10142 | 3 | 5173 / 517 |
| WIF/WSOL | `8Ve9KtGNtLRxCQNAVfkHEP5GRZHjdj6BjB1RQFZewG6V` | 0.01 WSOL | 5.474133 WIF | 119 | 3 | 40253 / 4024 |

### V2 — 报价依赖账户实时触发

V2 最后一关已经真实执行，不再只订阅 Pool Account。

最终验收 Run `31687579494`：

- 构建 **6 个可报价池**。
- 去重后订阅 **31 个报价依赖账户**。
- 其中 **22 个非 Pool 依赖账户**被作为关键触发目标，包括 vault、TickArray、Oracle、BinArray、bitmap extension 等。
- 真实收到 Orca TickArray 更新：
  - address=`2qJr7TWGCw3qdHXSfez1YftQcyqyfDVdVGbvvJNQZkPz`
  - slot=`438994790`
  - subscription=`6868142`
  - affected pool=`5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9`
- `subscription/address → dependency → affected pool` 映射正确识别为 Orca `TickArray`。
- 收到更新后真实重新计算 BONK/WSOL Orca Quote：0.01 WSOL → **332,306.43634 BONK**。
- 重算后刷新动态依赖集合，并验证该池没有缺失依赖账户。

最终程序日志明确输出：`V2 dependency-triggered quote recompute verified; refreshed dependency set is complete`。

### V2 最终完整回归

CI Run `31687579494`，已读取完整 Job 日志核对，不仅依赖 GitHub 的绿色状态：

- `cargo fmt` ✅
- `cargo check` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- 单元测试：**78 passed / 0 failed** ✅
- V0 Pool Discovery ✅
- V0 链上 owner 校验 ✅
- V1 Helius HTTP/WSS ✅
- Raydium Standard 本地 Quote ✅
- Orca Whirlpool 本地 Quote ✅
- Meteora DLMM 本地 Quote ✅
- 非 Pool 报价依赖账户真实 WSS 更新 ✅
- 更新映射到正确池并触发 Quote 重算 ✅
- 动态依赖刷新后完整性检查 ✅

因此 V2 已满足原定成功标准，正式进入 V3。

### CI — Cargo 缓存与依赖锁定

V3 开始前完成了一次独立基础设施优化，并保持业务代码不变。

- `Cargo.lock` 已生成并提交，Rust 依赖版本现在有可重复的锁定文件。
- CI 使用 `actions/cache@v5` 缓存 `~/.cargo/registry`、`~/.cargo/git` 和 `target`。
- 缓存主 key 同时绑定 runner OS、`rustc` 版本、`Cargo.toml` 与 `Cargo.lock`；Rust 版本或依赖配置变化时会生成新 key。
- 同一分支快速连续提交时，CI 通过 `concurrency` 取消旧的进行中任务，只保留最新提交，减少 Actions 时间浪费。
- 首次填充 Run `31688820193`：缓存 miss，完整回归成功并保存约 **958 MB** 缓存；冷启动 `cargo check` 约 **2 分 35 秒**，`cargo test` 约 **2 分 51 秒**。
- 验证 Run `31689455003` Attempt 2：命中完全相同的主 key；缓存恢复后 `cargo check` **4.20 秒**、`cargo clippy` **3.50 秒**、`cargo test` **4.55 秒**，单元测试仍为 **78 passed / 0 failed**。
- 该缓存约 **958 MB**，当次恢复和解压约 27 秒；虽然仍有固定恢复成本，但相比重复编译数分钟明显更低。
- 缓存命中后再次完成真实 Pool Discovery、主网 owner、Helius HTTP/WSS、Raydium、Orca、Meteora 和依赖账户 WSS 全链路回归。
- 临时生成 `Cargo.lock` 的 one-shot workflow 已删除，仓库只保留正式 CI。

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

处理方式：保留真实 CI 错误，核对当前结构，修改解析器，并增加回归测试确保旧结构不会被静默接受。

### 大量灰尘池

真实 discovery 会返回大量 TVL 极低的池。直接全部进入 WSS 订阅会浪费配额，也降低后续状态管理质量。

处理方式：增加可单独测试的 `select_monitoring_candidates`，当前按 TVL 下限和每 DEX 数量上限筛选。

### API 返回地址与链上真实性之间的边界

最初只能确认“DEX API 返回了这个地址”。V0 后续新增 Solana `getMultipleAccounts` 校验：候选账户必须存在，并且账户 `owner` 必须等于预期 DEX Program，否则测试失败。最终 18 个候选池全部通过。

### V1 Helius WSS 首次真实连接触发 Rustls CryptoProvider panic

第一次注入真实 Helius Secret 后，HTTP 已成功，但 WSS 在 TLS 初始化时 panic。根因是当前依赖组合没有提供唯一的 Rustls 加密后端。

处理方式：显式启用 `rustls` 的 `ring` CryptoProvider，不绕过 panic、不关闭 TLS 校验。修复后完整回归成功，并真实收到 Pool Account 更新。

### V2 只订阅 Pool Account 会漏掉报价变化

进入真实状态解析后确认：Swap 的关键报价状态可能存在于 vault、TickArray、BinArray 等依赖账户中，Pool Account 本身不保证每次都提供足够的实时触发信号。

因此 V2 建立了 `Pool → dependency accounts → reverse index → QuoteState`，最终通过真实 Orca TickArray 更新验证了非 Pool 依赖账户触发 Quote 重算。

### V2 出现过一次“CI 绿色但实际测试没跑”的假阳性

这是 V2 最重要的测试纪律问题，保留完整记录。

当时 CI 步骤执行 `cargo run -- dependency-wss-check` 后显示 `success`，但完整日志实际只打印了旧 `src/main.rs` 的 `Usage`。原因是：

1. 新 V2 命令逻辑已经写在 `src/app.rs`，但真正的主二进制仍由旧 `src/main.rs` 驱动。
2. 旧入口不认识 `dependency-wss-check`。
3. 未知命令仅打印 Usage 并返回退出码 0，导致 GitHub Actions 把“什么都没测试”标为成功。

处理方式：

- 将 `src/main.rs` 改为真正调用 `app::run()` 的薄入口。
- 新增 `AppCommand` + `parse_command`，明确路由所有命令。
- 未知命令现在返回错误，不允许再以退出码 0 假成功。
- 新增回归测试 `command_parser_accepts_dependency_wss_and_rejects_unknown_command`。
- 清理入口切换后 Clippy 暴露的无用生产代码，不使用 `allow` 掩盖。
- 重新执行完整 CI，并人工读取最终 Job 日志确认 `dependency-wss-check` 真实订阅了 31 个依赖账户、收到了真实 TickArray 更新并重算 Quote。

**因此 Run `31687579494` 才是 V2 的最终有效验收；之前的绿色状态不作为 V2 完成证据。**

### V2 测试本身也必须验证“测对了东西”

一次测试输入曾让“预期 program id 缺失”测试因为另一种错误而失败。虽然结果仍是 `is_err()`，失败机制已经偏离测试目标。该问题被主动发现并修复。

这条规则继续保留：**测试通过/失败本身不够，还要确认断言覆盖的是目标机制。**

### Cargo 缓存验证时出现一次 `minContextSlot` 短暂失败

缓存命中的首次完整回归 Run `31689455003` Attempt 1 在 Raydium 真实 Quote 阶段收到 Solana RPC `-32016: Minimum context slot has not been reached`。

该错误表示 RPC 节点当时尚未同步到程序要求的最小 slot。这里没有降低 `minContextSlot`、没有回退读取旧状态，也没有为让 CI 变绿而修改业务代码；直接重跑同一 Job 后 Attempt 2 全链路成功。

这说明 `minContextSlot` 的一致性保护按预期拒绝了落后节点，同时也说明真实联网 CI 存在外部节点短暂滞后的非确定性，后续若频率增加再单独设计有限重试策略。

## 当前问题 / 阻塞 / 风险

- 当前开发主要依赖 GitHub Actions 进行 Rust 编译和真实联网测试，因为本次 ChatGPT 执行环境没有可直接使用的 Rust toolchain。
- Cargo 缓存已经启用并真实命中；当前缓存归档约 **958 MB**，恢复/解压仍约需 27 秒，但相比数分钟冷编译明显更低。缓存 key 已绑定 Rust 版本和依赖锁文件，避免跨不兼容工具链静默复用。
- `actions/checkout@v4` 当前有 Node.js 20 弃用警告；GitHub 强制使用 Node 24，本项目 CI 仍成功。该警告目前不影响业务验证。
- `HELIUS_API_KEY` 没有写入仓库或 Git 历史，仅由 GitHub Actions Secret 在运行时注入。
- DEX REST API 的 TVL 属于动态外部数据，不能视为链上实时成交价格。
- 当前可报价池只有 6 个：每个 Token 的 Raydium Standard / Orca Whirlpool / Meteora DLMM 各 1 个。Raydium CLMM、Meteora DAMM v2 尚未进入 Quote 引擎。
- Orca 当前真实可报价池均为经典 SPL Token 且非 Adaptive Fee；Token-2022 transfer fee 和 Adaptive Fee Oracle 尚无当前实池验证证据。
- **当前还没有任何证据证明跨池套利策略可盈利。** V3 才开始计算、记录和统计真实闭环机会。
- 当前 GitHub App 无权读取个人 Billing Usage API，因此 README 不记录猜测的 Actions 剩余额度；需要从 GitHub `Settings → Billing & Licensing` 查看真实账户用量。

## 下一步

V3 当前计划：

1. 先做统一的“单池双方向 Quote”接口，让三个 DEX 的输出进入同一可比较数据结构。
2. 实现同 Token 两池闭环：例如 `WSOL → BONK（池 A）→ WSOL（池 B）`，反方向也计算。
3. 为一个机会测试多个输入金额，先得到毛利润曲线，再逐步加入交易成本模型。
4. 把实时依赖账户更新接到 Opportunity Engine，只重算受影响池和相关 Token 的路径。
5. 记录真实机会的 slot、路径、金额、收益和持续情况；这一阶段仍然不接钱包、不下单。
6. 需要连续 24–72 小时采样时停止使用 GitHub Actions 当长期运行环境，转到常驻 Linux VPS。

## 开发与记录约定

- README、项目说明和代码注释可以使用中文。
- Rust 的变量名、函数名、结构体名、模块名保持英文。
- 注释重点解释“为什么这样做”“链上字段代表什么”“风险在哪里”，避免给显而易见的代码逐行加注释。
- 每个有实际逻辑的函数都设计对应测试；依赖真实外部服务的函数再增加端到端真实测试。
- 每完成一个阶段，README 更新阶段状态、验证结果和下一步。
- 遇到会影响推进的真实问题时，立即写入“当前问题 / 阻塞 / 风险”，解决后保留必要的解决记录。
- **绿色 CI 只是信号；关键端到端步骤还要检查日志是否真的执行了目标逻辑。**
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
- V0 Pool Discovery 完成并验证；BONK/WIF 共 18 个候选 Pool Account 完成链上存在性与 owner 核验。
- V1 Helius HTTP/WSS 与 18 个 Pool Account 实时订阅完成并验证。
- V2 Raydium Standard AMM v4 本地 Quote 完成并真实验证。
- V2 Orca Whirlpool 官方 Core Quote 完成并真实验证。
- V2 Meteora DLMM 官方 Rust Quote 完成并真实验证。
- 修复 `dependency-wss-check` 旧入口导致的 CI 假阳性，并增加未知命令失败回归测试。
- V2 最终 CI Run `31687579494`：**78 passed / 0 failed**；31 个依赖账户实时订阅；真实 Orca TickArray 更新成功映射并触发 Quote 重算。
- **V2 正式完成，项目进入 V3。**
- 提交 `Cargo.lock`，固定当前 Rust 依赖解析结果。
- CI 加入 Cargo registry/git/target 缓存和同分支并发取消策略；首次缓存约 **958 MB**。
- Run `31689455003` Attempt 2 真实 cache hit：`cargo check` 4.20 秒、`cargo test` 4.55 秒，78 个测试和全部真实联网回归通过。
- 缓存命中回归期间真实收到 Meteora `BinArray` 更新并触发 Quote 重算，说明基础设施优化没有破坏 V2 实时链路。