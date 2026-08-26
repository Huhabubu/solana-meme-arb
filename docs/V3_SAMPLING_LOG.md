# V3 长时采样日志

本文件只记录 V3 连续监控的真实运行证据。README 继续作为项目总仪表盘。

## 2026-08-15 — 两小时 artifact 一致性复核与修复

对 Attempt 2 的 60,252 条 JSONL 做独立复核后确认：

- 47 条 `net_positive` 中有 29 条的两腿 slot 不同，最大跨度 217 slots。
- 按 route + amount 合并后只有 15 段正值；每一段在同一路径的下一次观察中都转为非正，间隔约 83–3,191 ms。
- 同一 slot 内也出现依赖账户依次到达后利润正负翻转，符合 WebSocket 单账户通知造成的部分状态混合。

因此旧样本中的正值不能解释为可执行套利。修复后的 monitor 把 WSS 仅作为触发信号，为相关路径建立同一 RPC context slot 的一致快照；超过 100 个账户时自动分片并重试到同一 context slot。若首次结果为正，首次观察先原样落盘，再跨到下一 slot 复核并追加第二次观察。旧 artifact 保留为缺陷证据，不与修复后的样本合并统计。

同时完成：Pool Universe 改为支持池型过滤后按每 DEX TVL Top-N 选择，并用 `.universe` 清单绑定每份样本；JSONL 改为流式重放并仅恢复未写完的最后一行；配置模板统一为 `HELIUS_API_KEY`。

## 2026-08-14 — GitHub 2h 采样 Attempt 1

Run：`31777230480`

目标：在 VPS 准备完成前，先用 GitHub-hosted Linux runner 连续运行 `opportunity-monitor` 2 小时。

实际运行：**14分51秒** 后失败。

采样结果：

- processed WSS updates：**586**
- JSONL records：**7,884**
- JSONL 大小：约 **5.4 MB**
- evaluated：**7,352**
- insufficient liquidity：**532**
- gross-positive：**6**
- net-positive（当前 6,000 lamports 成本下界）：**6**
- 峰值 RSS：**20,292 KB（约 19.8 MiB）**
- artifact ID：`9210669630`

失败点：

```text
Solana RPC error -32016: Minimum context slot has not been reached
```

触发场景是 Orca PoolState WSS update 后，依赖刷新继续要求 HTTP RPC 满足该 update 的 `minContextSlot`；RPC 层短暂重试耗尽后，旧 monitor 将该错误向上抛出并终止整个进程。

结论：

- `minContextSlot` 一致性保护本身正确，不能为了长跑而降级读取旧状态。
- 长期 monitor 需要把“节点暂时落后”视为可恢复运行事件，而不是进程级致命错误。
- 第一份连续样本已经首次观察到 6 个 gross-positive / net-positive probe records，但在分析 artifact 前不能把它们解释为真实可执行套利。

## 2026-08-14 — `-32016` 长期恢复修复

源码 commit：`a690b4ab5f880acb88ff439573d745bf692a4c5f`

实现：

1. RPC full-account response 使用可机器识别的 typed error 保存 JSON-RPC code。
2. `is_min_context_slot_not_reached()` 即使错误被 `anyhow::Context` 包裹，也能识别 `-32016`。
3. RPC 层仍保留原有短暂有限重试，不降低 `minContextSlot`。
4. 长期 `opportunity-monitor` 若短重试仍耗尽：
   - 增加 `context_slot_recoveries`；
   - 等待 1 秒；
   - 重建最新 `QuoteState + QuoteContextCache`；
   - 重建 WSS session 后继续监控；
   - 当前无法一致处理的那条 update 不写入 JSONL，避免用旧状态伪造机会。
5. 其他错误仍保持失败，不做宽泛吞错。

Guarded source verification：`fmt/check/clippy/tests` 全部通过后才提交。

最终正式 CI：Run `31784587520`，全部成功，包括：

- fmt / check / Clippy / tests
- Pool Discovery / Mainnet owner
- Helius HTTP/WSS
- Raydium / Orca / Meteora live Quote
- V3 全路径多金额
- Opportunity JSONL persistence
- V3.6.2 continuous WSS monitor E2E

## 2026-08-14 — GitHub 2h 采样 Attempt 2

Run：`31784845211`

Commit：`5f48f642787eef590eff183858ad8ece7d270687`

状态：**✅ 完整跑满并成功退出**。

配置：

```text
OPPORTUNITY_MONITOR_MAX_SECONDS=7200
OPPORTUNITY_MONITOR_UPDATE_TIMEOUT_SECONDS=45
OPPORTUNITY_MONITOR_MAX_RECONNECTS=20
```

实际运行：**2:00:35**。

最终 monitor 汇总：

```text
processed_updates=4524
appended_records=60252
total_records=60252
connected_sessions=16
reconnects=6
context_slot_recoveries=0
subscription_refreshes=10
duplicate_updates=0
stale_updates=0
max_updates_in_single_session=1413
evaluated=55974
insufficient=4278
gross_positive=189
net_positive=47
```

资源与数据量：

- JSONL：**60,252 行**
- JSONL 原始大小：约 **41 MiB**
- 峰值 RSS：**60,576 KB（约 59.2 MiB）**
- artifact ID：`9216349069`
- artifact size：`1,526,425` bytes（压缩后）
- artifact digest：`sha256:0629bf633e56fdbcb8355ab6d0d637e8ce12e7954735ea0cf3da30ce33798efb`

### 可靠性结论

- 2 小时内没有再次出现 `-32016`，因此这次没有实际触发 `context_slot_recoveries`；恢复分支已由单测/正式 CI 验证，但仍需要更长 VPS 样本覆盖真实触发。
- WSS 共建立 16 个 session，发生 6 次普通重连和 10 次动态依赖订阅刷新。
- `duplicate_updates=0`、`stale_updates=0`。
- 单个 session 最多连续处理 **1,413** 个 update。
- monitor 跑满时限后正常退出，JSONL 与最终统计一致。

### artifact 初步机会分析

实际下载并解析 60,252 条 JSONL 后：

- 47 个 `net_positive` probe records 中，BONK **43**，WIF **4**。
- 这些记录只分布在 **5 个 trigger slot**，说明同一市场状态会因多个依赖账户连续更新而被重复评估。
- 因此 **47 不能解释为 47 次独立套利机会**。后续机会统计必须按市场状态/slot/route 做 episode 去重与持续时间分析。
- 当前最大观察到的单条净利润 probe 为 BONK `Meteora DLMM → Orca`、输入 `0.1 SOL`，`net_profit_raw=121,780 lamports`；但成本仍只是 6,000 lamports 研究下界，不能直接视为真实可执行利润。
- WIF 的正净利润记录很少，当前样本中只在 `Orca → Raydium` 的部分金额点出现。

### 当前解释边界

这 2 小时样本已经证明：

1. 常驻 monitor 可以在 GitHub-hosted Linux 上连续工作 2 小时；
2. 实际市场中确实会出现正毛利润/正“研究净利润” probe；
3. 正值高度成簇，事件级计数会显著高估独立机会；
4. 在进入 V4 前，还需要把 **opportunity episode 去重、持续时间、真实执行成本/延迟** 纳入分析；
5. 2 小时仍不能替代 VPS 24–72 小时长期稳定性样本。
