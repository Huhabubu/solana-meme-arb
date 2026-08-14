# V3 长时采样日志

本文件只记录 V3 连续监控的真实运行证据。README 继续作为项目总仪表盘。

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

状态：**正在运行**。

配置：

```text
OPPORTUNITY_MONITOR_MAX_SECONDS=7200
OPPORTUNITY_MONITOR_UPDATE_TIMEOUT_SECONDS=45
OPPORTUNITY_MONITOR_MAX_RECONNECTS=20
```

每 60 秒 heartbeat 记录：

```text
elapsed
JSONL records
JSONL bytes
context_slot_recoveries
```

验收重点：

- 是否跑满约 2 小时；
- `-32016` 出现后是否转为 `context_slot_recoveries` 并继续运行；
- reconnect / subscription refresh / duplicate / stale 的频率；
- gross-positive / net-positive 的数量与持续性；
- JSONL 增长速度与峰值 RSS。
