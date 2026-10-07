# 自动化数据周报 & 竞品动态监控系统

两条相互独立的管道：将市场原始数据与竞品公告，转化为可直接决策的报告，自动推送到 **Lark（飞书）** 与 **Gmail**，并在本地归档 HTML、发送 macOS 运行通知。

> English docs: [README.md](README.md)。

---

## 一、项目架构

```
environment.env ──► 两个独立管道
  ├── weekly_report 管道（日报/周报/月报）
  │     weekly_data_fetcher  (Binance 现货/合约 API + 内部指标建模)
  │         └─► weekly_report_generator (DeepSeek 结构化输出 → HTML)
  └── competitor_monitor 管道（每日）
        competitor_fetcher   (Binance / OKX / Bybit 官方公告)
        competitor_analyzer  (LLM 影响优先级评估)
        competitor_report_generator (HTML)
共享模块：database.py(SQLite) · llm_client.py(DeepSeek) · lark_notifier.py · email_notifier.py · mac_notifier.py
```

### 目录结构
```
src/           核心模块（config/database/llm_client/lark/email/mac + 抓取/分析/生成/pipeline）
templates/     weekly_report.html / competitor_report.html
scripts/       run_weekly.py / run_competitor.py / run_all.py
reports/       生成的 HTML（已 gitignore）
docs/          development-log.md / insights.md
Dockerfile · docker-compose.yml · environment.env.example
```

---

## 二、快速开始

```bash
conda activate report_competitor_monitor     # Python 3.11 环境
pip install -r requirements.txt
cp environment.env.example environment.env   # 填入 DeepSeek Key、Lark Webhook、Gmail
python scripts/run_all.py                    # 首次运行：三种报告 + 竞品报告各跑一次
open reports/weekly_report_*.html
```

`environment.env` 已被 gitignore；任何留空的配置只会让对应通道降级为「跳过」，不会导致运行失败。

---

## 三、运行命令

| 命令 | 说明 |
|---|---|
| `python -m src.pipeline report --type auto` | 每日都跑日报；周一加跑周报；每月 1 号加跑月报 |
| `python -m src.pipeline report --type all` | 强制生成 日/周/月 三种 |
| `python -m src.pipeline competitor` | 抓取 → 归因 → 报告（每日） |
| `python -m src.pipeline all` | 首次全量运行 |
| `python scripts/run_*.py` | 对应封装脚本 |

**推送频率依据报告名字**：日报每天、周报周一、月报每月 1 号；竞品报告每天推送。邮件同频，且竞品报告单独成信，不与数据报告合并。

---

## 四、Lark（飞书）机器人配置

系统使用**两个独立群机器人**（周报机器人、竞品机器人）：

1. 目标群 → 设置 → 群机器人 → 添加「自定义机器人」。
2. 复制 Webhook，填入 `environment.env`：
   ```dotenv
   LARK_WEBHOOK_WEEKLY=...       # 数据周报机器人
   LARK_WEBHOOK_COMPETITOR=...   # 竞品机器人
   ```
3. 卡片格式：标题栏 + Markdown 正文 + 跳转按钮（链接到完整 HTML 报告），另附一条运行成功/失败文本通知。

每次运行同时触发 **macOS 系统通知**，内容包含：一句话结论 · 运行状态 · 结束运行时间 · 邮件推送状态 · Lark 推送状态。

---

## 五、Gmail 推送（OAuth）

1. Google Cloud Console 创建项目并启用 **Gmail API**。
2. 创建 **OAuth 客户端 ID → 桌面应用**，下载 JSON 到 `credentials/client_secret.json`。
3. 配置 `GMAIL_SENDER` 与 `GMAIL_RECIPIENTS`（可逗号分隔多个，支持邮箱组）。
4. 首次运行会打开浏览器授权，生成 `credentials/token.json`；此后静默刷新，服务器上可无人值守。无浏览器服务器：本机授权后把 `token.json` 拷过去即可。

---

## 六、调度与自动化

- **Cline Schedule**：每天 09:00 运行 `report --type auto`；每天 09:00 运行 `competitor`。日报每天产出，周报自动在周一 09:00 产出，月报在每月 1 号产出（此设定同时覆盖了“每周一运行周报管道”的需求）。
- **服务器 cron**：
  ```cron
  0 9 * * *  cd /opt/report_competitor_monitor && /opt/conda/envs/report_competitor_monitor/bin/python -m src.pipeline report --type auto
  0 9 * * *  cd /opt/report_competitor_monitor && /opt/conda/envs/report_competitor_monitor/bin/python -m src.pipeline competitor
  ```
- 每次运行：生成 HTML → 推送 Lark 卡片与状态 → 发送邮件（竞品单独一封）→ macOS 通知 → 写入 `run_log`。

---

## 七、Docker

```bash
docker compose build
docker compose --profile bootstrap up bootstrap   # 首次全量
docker compose up report-pipeline
docker compose up competitor-monitor
```

---

## 八、生产环境部署（公司云上）

本项目在本机即可运行，但**真正的数据服务不应跑在你的本地机器上**。公司生产环境请部署到云服务器（**阿里云 ECS / 腾讯云 CVM** 等），在云上长期定期运行：

1. 开通一台常驻小规格实例（1–2 vCPU 足够）。
2. 安装运行时：Docker + docker compose（最简）或 Miniconda + `report_competitor_monitor` 环境。
3. 克隆仓库，创建 `environment.env` 写入生产密钥（建议接入云厂商密钥管理）。
4. 用 Nginx 将 `reports/` 目录对外提供访问，并设置 `REPORT_BASE_URL=https://reports.公司域名`，让 Lark 卡片按钮可打开线上 HTML。
5. 用 cron 或 systemd timer 定时运行（见第六节）。
6. **配置干系人邮箱组**：把 stakeholders 的邮箱或邮件组写入 `GMAIL_RECIPIENTS`，确保每份报告送达整个群体。
7. 首次完成 Gmail 授权后，服务器即可自动刷新令牌。

完成上述配置后，本项目就成为一个**云上自运行的数据服务**，持续输出日报/周报/月报与每日竞品报告，不再依赖你的本地电脑。

---

## 九、说明

- 外部公告接口经常变动，适配器在抓取失败时会退回内置样例数据，保证报告始终可产出，并记录告警。
- 内部指标（VIP 分层、Earn 净流入、注册数）为**确定性建模**数据，接入生产数据时替换 `weekly_data_fetcher._simulated_internal` 即可。
- 设计取舍见 [`docs/development-log.md`](docs/development-log.md)；业务洞察见 [`docs/insights.md`](docs/insights.md)。
