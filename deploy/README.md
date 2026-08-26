# Linux VPS 长时监控部署

本目录用于 V3 的 24–72 小时真实采样。当前服务只做研究监控：**不需要钱包、私钥或助记词，也不会提交交易**。

## 目录约定

```text
/opt/solana-meme-arb/solana-meme-arb        # release 二进制
/etc/solana-meme-arb/monitor.env             # Helius Key 与 monitor 配置，权限 600
/var/lib/solana-meme-arb/opportunities.jsonl # 长时采样数据
/etc/systemd/system/solana-meme-arb.service  # systemd 服务
```

## 0. 选择正确 CPU 架构

`release-build` 会为 Linux 原生生成两个 artifact：

```text
solana-meme-arb-linux-x86_64-<commit>
solana-meme-arb-linux-aarch64-<commit>
```

按 VPS 的 CPU 选择：

| VPS CPU | 下载 artifact |
|---|---|
| AMD / Intel | `x86_64` |
| ARM / Ampere | `aarch64` |

服务器可执行：

```bash
uname -m
```

典型输出：

```text
x86_64   → 下载 x86_64 artifact
aarch64  → 下载 aarch64 artifact
```

不要把错误架构的二进制安装到服务器。

### 双架构已验证样本

2026-08-14 Run `31771245684` 已真实验证同一 commit `48e3e51c14996a3374c9f824f9220d63e1380057`：

- x86_64：native `ubuntu-24.04` runner，release build + **114/114 tests** + `file` x86-64 断言 + artifact。
- ARM64：native `ubuntu-24.04-arm` runner，release build + **114/114 tests** + `file` ARM aarch64 断言 + artifact。
- 两个 artifact 都实际下载并执行 `sha256sum -c SHA256SUMS`，均为 `solana-meme-arb: OK`。

以后部署仍应针对当前目标 commit 重新运行 `release-build`，不要长期复用过期 artifact。

## 1. 获取已验证 release bundle

在 GitHub Actions 手动运行 `release-build`。每个架构都会执行：

```text
cargo build --release --locked
        ↓
cargo test --release --locked --all-targets
        ↓
file 架构断言
        ↓
打包二进制 + systemd + env 模板 + DEPLOY.md
        ↓
SHA256SUMS
        ↓
Actions artifact
```

下载与你服务器 CPU 对应的 artifact 并解压。目录至少包含：

```text
solana-meme-arb
solana-meme-arb.service
monitor.env.example
DEPLOY.md
BUILD-INFO.txt
BINARY-FILE.txt
SHA256SUMS
```

先验证：

```bash
sha256sum -c SHA256SUMS
cat BUILD-INFO.txt
cat BINARY-FILE.txt
```

只有 `sha256sum` 输出：

```text
solana-meme-arb: OK
```

且 `BINARY-FILE.txt` 的 CPU 架构与 `uname -m` 对应，才继续部署。

## 2. 创建专用系统用户和目录

```bash
sudo useradd --system --home /var/lib/solana-meme-arb --shell /usr/sbin/nologin solana-arb
sudo install -d -o solana-arb -g solana-arb /var/lib/solana-meme-arb
sudo install -d -m 0755 /opt/solana-meme-arb
sudo install -d -m 0750 /etc/solana-meme-arb
```

如果 `solana-arb` 已存在，不要重复创建。

## 3. 安装 release 二进制

```bash
sudo install -m 0755 solana-meme-arb /opt/solana-meme-arb/solana-meme-arb
```

生产采样不在低内存 VPS 上重新解析/编译整套 Rust 依赖。仓库已提交 `Cargo.lock`，release workflow 使用 `--locked`。

## 4. 配置 Helius Key

```bash
sudo cp monitor.env.example /etc/solana-meme-arb/monitor.env
sudo chmod 600 /etc/solana-meme-arb/monitor.env
sudo editor /etc/solana-meme-arb/monitor.env
```

把：

```text
HELIUS_API_KEY=REPLACE_WITH_HELIUS_API_KEY
```

替换为真实 Key。

不要把 `/etc/solana-meme-arb/monitor.env` 上传到 GitHub。

模板默认设置：

```text
OPPORTUNITY_MONITOR_MAX_SECONDS=259200
```

即采样运行 72 小时后正常退出；`Restart=on-failure` 不会把正常到期当成故障重启。`OPPORTUNITY_MONITOR_UPDATES` 与 `OPPORTUNITY_MONITOR_MAX_RECONNECTS` 默认不设置，程序内部仍持续做有界退避重连，真正不可恢复退出时由 systemd 拉起。

两小时旧样本约 41 MiB；部署前建议确认 `/var/lib/solana-meme-arb` 至少有 3 GiB 可用空间，并在采样过程中监控磁盘占用。

## 5. 安装并启动 systemd

```bash
sudo cp solana-meme-arb.service /etc/systemd/system/solana-meme-arb.service
sudo systemctl daemon-reload
sudo systemctl enable --now solana-meme-arb
```

检查：

```bash
sudo systemctl status solana-meme-arb --no-pager
sudo journalctl -u solana-meme-arb -n 100 --no-pager
```

实时日志：

```bash
sudo journalctl -u solana-meme-arb -f
```

## 6. 验证 JSONL 正在增长

```bash
sudo ls -lh /var/lib/solana-meme-arb/opportunities.jsonl
sudo wc -l /var/lib/solana-meme-arb/opportunities.jsonl
```

数据是 append-only JSONL，每条 `OpportunityRecord` 一行。采样期间不要手工编辑。

## 6.1 运行延迟基准

Release 二进制内置 `latency-bench` 命令。它复用正式 monitor 的 WSS、RPC、快照、报价和 JSONL 落盘链路，输出每个阶段的 p50/p95 延迟（p50 是中位数，p95 是较慢尾部的 95 分位），单位为微秒。

为避免与常驻服务重复订阅，先停止服务；在受保护的终端中加载已有配置，再使用独立日志文件运行：

```bash
sudo systemctl stop solana-meme-arb
set -a
. /etc/solana-meme-arb/monitor.env
set +a
export OPPORTUNITY_LOG_PATH=/var/lib/solana-meme-arb/latency-bench.jsonl
export OPPORTUNITY_MONITOR_UPDATES=20
export OPPORTUNITY_MONITOR_MAX_SECONDS=1800
sudo runuser -u solana-arb --preserve-environment -- /opt/solana-meme-arb/solana-meme-arb latency-bench
sudo systemctl start solana-meme-arb
```

输出中的 `end_to_end_us_p50/p95` 是从 WSS 更新排队、处理到持久化完成的近似端到端时间；`queue_delay`、`snapshot`、`quote` 等字段用于定位瓶颈。基准文件应在完成分析后按采样文件流程归档或清理。

## 7. 结束 24–72 小时采样并取回数据

先停止服务，得到静止文件：

```bash
sudo systemctl stop solana-meme-arb
sudo wc -l /var/lib/solana-meme-arb/opportunities.jsonl
```

复制到普通用户可下载位置：

```bash
sudo cp /var/lib/solana-meme-arb/opportunities.jsonl /tmp/opportunities.jsonl
sudo chown "$USER":"$USER" /tmp/opportunities.jsonl
```

下载 `/tmp/opportunities.jsonl` 后用 Python/pandas 做 V3 长时统计。

重新开始：

```bash
sudo systemctl start solana-meme-arb
```

如果要开启一个全新的独立采样窗口，应在服务停止时先归档旧 JSONL，再启动。

## 8. 更新二进制

针对目标 commit 重新运行 `release-build`，下载服务器对应架构的 artifact，再执行：

```bash
sha256sum -c SHA256SUMS
```

确认 `OK` 后：

```bash
sudo systemctl stop solana-meme-arb
sudo install -m 0755 solana-meme-arb /opt/solana-meme-arb/solana-meme-arb
sudo systemctl start solana-meme-arb
sudo systemctl status solana-meme-arb --no-pager
```

不要把“服务运行时直接覆盖二进制”作为常规部署流程。

## systemd 与程序的职责边界

程序负责：

- Helius WSS 长连接；
- duplicate / stale update 过滤；
- 可恢复 WSS 断线的有界退避重连；
- 依赖集合变化后的订阅重建；
- affected Context / route 重算；
- OpportunityEvent → JSONL append；
- 增量统计。

systemd 负责：

- VPS 重启后自动启动；
- 程序遇到不可恢复错误退出时 `Restart=on-failure`；
- 统一收集 stdout/stderr 到 journal。

## 安全边界

- `/etc/solana-meme-arb/monitor.env` 权限 `600`。
- 服务使用无登录权限的 `solana-arb` 用户。
- `/opt` 与 `/etc` 保持只读；只允许 `/var/lib/solana-meme-arb` 写入。
- 本阶段不配置钱包、私钥、助记词或交易签名材料。
