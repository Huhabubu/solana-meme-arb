# Linux VPS 长时监控部署

本目录用于 V3 的 24–72 小时真实采样。当前服务只做研究监控：**不需要钱包、私钥或助记词，也不会提交交易**。

## 目录约定

```text
/opt/solana-meme-arb/solana-meme-arb       # release 二进制
/etc/solana-meme-arb/monitor.env            # Helius Key 与 monitor 配置，权限 600
/var/lib/solana-meme-arb/opportunities.jsonl # 长时采样数据
/etc/systemd/system/solana-meme-arb.service  # systemd 服务
```

## 1. 创建专用系统用户和目录

```bash
sudo useradd --system --home /var/lib/solana-meme-arb --shell /usr/sbin/nologin solana-arb
sudo install -d -o solana-arb -g solana-arb /var/lib/solana-meme-arb
sudo install -d -m 0755 /opt/solana-meme-arb
sudo install -d -m 0750 /etc/solana-meme-arb
```

如果 `solana-arb` 用户已经存在，`useradd` 会报已存在；不要重复创建即可。

## 2. 安装 release 二进制

部署时使用已经通过 GitHub Actions `cargo build --release --locked` 的版本。把二进制上传到服务器后：

```bash
sudo install -m 0755 solana-meme-arb /opt/solana-meme-arb/solana-meme-arb
```

不要在低内存 VPS 上临时重新解析依赖或随意升级 crate。仓库已经提交 `Cargo.lock`，生产构建必须使用 `--locked`。

## 3. 配置 Helius Key

把 `deploy/monitor.env.example` 复制为服务器配置：

```bash
sudo cp deploy/monitor.env.example /etc/solana-meme-arb/monitor.env
sudo chmod 600 /etc/solana-meme-arb/monitor.env
sudo editor /etc/solana-meme-arb/monitor.env
```

将：

```text
HELIUS_API_KEY=REPLACE_WITH_HELIUS_API_KEY
```

替换为真实 Key。不要加到仓库，不要把 `/etc/solana-meme-arb/monitor.env` 上传到 GitHub。

常驻模式默认不设置 `OPPORTUNITY_MONITOR_UPDATES` 和 `OPPORTUNITY_MONITOR_MAX_SECONDS`，因此程序不会因计数或总时长主动退出。

## 4. 安装 systemd 服务

```bash
sudo cp deploy/solana-meme-arb.service /etc/systemd/system/solana-meme-arb.service
sudo systemctl daemon-reload
sudo systemctl enable --now solana-meme-arb
```

检查状态：

```bash
sudo systemctl status solana-meme-arb --no-pager
```

实时查看日志：

```bash
sudo journalctl -u solana-meme-arb -f
```

查看最近 100 行：

```bash
sudo journalctl -u solana-meme-arb -n 100 --no-pager
```

## 5. 验证 JSONL 正在增长

```bash
sudo ls -lh /var/lib/solana-meme-arb/opportunities.jsonl
sudo wc -l /var/lib/solana-meme-arb/opportunities.jsonl
```

程序采用 append-only JSONL；每条 `OpportunityRecord` 独占一行。不要在采样期间手工编辑该文件。

## 6. 停止 24–72 小时采样并取回数据

为了得到一个完整、静止的分析文件，采样结束时先停止服务：

```bash
sudo systemctl stop solana-meme-arb
sudo wc -l /var/lib/solana-meme-arb/opportunities.jsonl
```

再复制数据：

```bash
sudo cp /var/lib/solana-meme-arb/opportunities.jsonl /tmp/opportunities.jsonl
sudo chown "$USER":"$USER" /tmp/opportunities.jsonl
```

把 `/tmp/opportunities.jsonl` 下载后，可用 Python/pandas 做 V3 长时统计。

重新开始监控：

```bash
sudo systemctl start solana-meme-arb
```

注意：程序会继续 append 原文件。如果需要开始一个全新的独立采样窗口，应在服务停止状态下先把旧文件重命名归档，再启动服务。

## 7. 更新二进制

先在 GitHub Actions 验证新 commit。服务器更新时：

```bash
sudo systemctl stop solana-meme-arb
sudo install -m 0755 solana-meme-arb /opt/solana-meme-arb/solana-meme-arb
sudo systemctl start solana-meme-arb
sudo systemctl status solana-meme-arb --no-pager
```

不要在服务运行时直接覆盖正在执行的二进制作为常规部署流程。

## systemd 的职责边界

程序内部负责：

- Helius WSS 长连接；
- duplicate / stale update 过滤；
- 可恢复 WSS 断线的有界退避重连；
- 依赖集合变化后的订阅重建；
- OpportunityEvent → JSONL append；
- 增量统计。

systemd 负责：

- VPS 重启后自动启动；
- 程序遇到不可恢复错误退出时 `Restart=on-failure`；
- 统一收集 stdout/stderr 到 journal。

## 当前安全边界

- `/etc/solana-meme-arb/monitor.env` 权限设为 `600`。
- systemd 使用无登录权限的 `solana-arb` 用户运行。
- `/opt` 二进制和 `/etc` 配置只读；仅 `/var/lib/solana-meme-arb` 允许服务写入。
- 本阶段不配置钱包、私钥、助记词或交易签名材料。
