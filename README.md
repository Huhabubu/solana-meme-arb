# solana-meme-arb

一个使用 Rust 开发的 Solana 成熟 Meme 跨 DEX 套利研究项目。

当前阶段以 **真实监控、真实验证** 为主。只有当监控数据证明机会具备可执行价值后，才会进入钱包、交易构造和实盘执行。

## 当前进度

**当前阶段：V0 — Pool Discovery（池发现）**

| 阶段 | 内容 | 状态 |
|---|---|---|
| Stage 0 | 仓库、Rust 工程骨架、GitHub Actions CI | ✅ 已完成并验证 |
| V0 | BONK / WIF 跨 DEX 池发现 | 🔄 当前进行 |
| V1 | Helius RPC/WSS 实时池账户订阅 | ⏳ 未开始 |
| V2 | DEX Pool State 解析与本地 Swap Quote | ⏳ 未开始 |
| V3 | 跨池套利机会计算、记录与统计 | ⏳ 未开始 |
| V4 | 原子交易构造与 simulateTransaction | ⏳ 未开始 |
| V5 | 小额实盘执行与 Jito 集成 | ⏳ 未开始 |

## 当前阶段成功标准

V0 只有满足以下条件才会标记完成：

1. 从真实数据源找到 BONK / WIF 在目标 DEX 上的池。
2. 核对并保存真实 Pool Account 地址、池类型和对应交易对。
3. 明确每项数据来自哪里；无法核验的数据不作为事实写入项目。
4. 新增代码通过 `cargo fmt`、`cargo check`、`cargo clippy` 和 `cargo test`。

## 已验证结果

### Stage 0

GitHub Actions 已在真实 runner 上完成以下检查：

- `cargo fmt --all -- --check` ✅
- `cargo check --all-targets` ✅
- `cargo clippy --all-targets -- -D warnings` ✅
- `cargo test --all-targets` ✅（1 passed / 0 failed）
- CI 总结果：`success`
- CI 当次实际 Rust 版本：`rustc 1.97.1`

## 当前问题 / 阻塞 / 风险

- 当前开发会主要依赖 GitHub Actions 进行 Rust 编译和测试，因为本次 ChatGPT 执行环境没有可直接使用的 Rust toolchain。
- `actions/checkout@v4` 当前在 GitHub runner 上有 Node.js 20 弃用警告；本次 CI 成功，不影响当前业务逻辑，后续单独处理。
- Helius API Key 只允许通过环境变量或 GitHub Secret 注入，禁止提交到仓库。
- 目前还没有任何真实套利收益数据，因此项目不会声明“策略可盈利”。

## 下一步

正在推进 V0：

1. 核对 BONK、WIF 的 Mint 地址。
2. 查询 Raydium、Orca、Meteora 的实际池。
3. 选出值得进入实时监听阶段的主要池。
4. 将池发现逻辑写入 Rust，并用 CI 验证。

## 开发与记录约定

- README、项目说明和代码注释可以使用中文。
- Rust 的变量名、函数名、结构体名、模块名保持英文。
- 注释重点解释“为什么这样做”“链上字段代表什么”“风险在哪里”，避免给显而易见的代码逐行加注释。
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

- Stage 0 完成。
- CI 首次真实通过。
- README 改为中文并作为项目持续进度看板。
- 开始 V0 Pool Discovery。
