# Automated Data Reports & Competitor Monitoring System

Two independent pipelines that turn raw market data and competitor
announcements into decision-ready reports, delivered automatically to Lark
(Feishu) and Gmail, with local HTML archives and macOS run notifications.

> 中文文档见 [README_zh.md](README_zh.md).

---

## 1. Architecture

```
                        ┌──────────────────────────────────────────┐
                        │            environment.env                │
                        └──────────────────────────────────────────┘
                                          │
        ┌─────────────────────────────────┴─────────────────────────────────┐
        │                                                                    │
        ▼                                                                    ▼
┌───────────────────────┐                                       ┌────────────────────────┐
│  weekly_report 管道    │                                       │ competitor_monitor 管道 │
│  (daily/weekly/monthly)│                                       │        (daily)          │
└───────────┬───────────┘                                       └────────────┬───────────┘
            │                                                                 │
   weekly_data_fetcher.py                                    competitor_fetcher.py
   (Binance spot/futures API                                 (Binance / OKX / Bybit
    + modelled internal KPIs)                                 announcements)
            │                                                                 │
            ▼                                                                 ▼
   weekly_report_generator.py                                competitor_analyzer.py (LLM)
   (DeepSeek schema prompt → HTML)                           competitor_report_generator.py
            │                                                                 │
            └───────────────┬───────────────────────────────┬────────────────┘
                            ▼                               ▼
                    ┌───────────────┐               ┌───────────────┐
                    │ shared modules│               │  outputs       │
                    │ database.py   │               │ reports/*.html │
                    │ llm_client.py │               │ SQLite state   │
                    └───────────────┘               └───────────────┘
                            │
        ┌───────────────────┼────────────────────┬───────────────────┐
        ▼                   ▼                    ▼                   ▼
  lark_notifier.py    email_notifier.py     mac_notifier.py     database.run_log
  (2 bots/cards)      (Gmail OAuth HTML)    (terminal-notifier)
```

### Project layout
```
├── src/
│   ├── config.py                     # env loader
│   ├── database.py                   # SQLite layer (metrics/events/state/run_log)
│   ├── llm_client.py                 # DeepSeek chat + JSON wrapper
│   ├── lark_notifier.py              # interactive card / text push
│   ├── email_notifier.py             # Gmail API + OAuth
│   ├── mac_notifier.py               # macOS desktop notifications
│   ├── weekly_data_fetcher.py        # Binance data + internal KPI modelling
│   ├── competitor_fetcher.py         # Binance/OKX/Bybit announcements
│   ├── weekly_report_generator.py    # report narrative + HTML
│   ├── competitor_analyzer.py        # LLM impact classification
│   ├── competitor_report_generator.py# competitor narrative + HTML
│   ├── analyzer_labels.py            # shared impact labels
│   └── pipeline.py                   # orchestration + CLI
├── templates/                        # Jinja2 HTML templates
├── scripts/                          # run_weekly / run_competitor / run_all
├── reports/                          # generated HTML (git-ignored)
├── docs/                             # development-log.md / insights.md
├── Dockerfile, docker-compose.yml
└── environment.env.example

---

## 2. Quick start

### 2.1 Environment
A dedicated conda env named **`report_competitor_monitor`** (Python 3.11):

```bash
conda env list                       # report_competitor_monitor should exist
conda activate report_competitor_monitor
pip install -r requirements.txt
```

### 2.2 Configure
```bash
cp environment.env.example environment.env
# edit environment.env — DeepSeek key, Lark webhooks, Gmail settings
```
`environment.env` is git-ignored. Every value has a sensible default; anything
left blank simply degrades that channel to "skipped" (the run still succeeds).

### 2.3 First run (bootstrap)
The first successful run generates **all three report types** plus the
competitor report so every output format is visible at once:

```bash
python scripts/run_all.py          # daily + weekly + monthly + competitor
```

Reports land in `reports/`. Open any of them in a browser:
```bash
open reports/weekly_report_*.html
```

---

## 3. Running the pipelines

| Command | What it does |
|---|---|
| `python -m src.pipeline report --type auto` | daily always; weekly on Mon; monthly on 1st |
| `python -m src.pipeline report --type all` | force daily + weekly + monthly |
| `python -m src.pipeline report --type weekly` | just the weekly report |
| `python -m src.pipeline competitor` | fetch → classify → report (daily) |
| `python -m src.pipeline all` | bootstrap everything |
| `python scripts/run_weekly.py` | wrapper for the report pipeline |
| `python scripts/run_competitor.py` | wrapper for the competitor pipeline |
| `python scripts/run_all.py` | wrapper for the bootstrap |

**Push cadence follows the report name**: `daily` pushes every day, `weekly`
pushes on Mondays, `monthly` on the 1st. The competitor report pushes **daily**.
Emails follow the same cadence; the competitor email is always sent separately,
never bundled with the data report emails.


---

## 4. Lark (Feishu) bot configuration guide

The system uses **two separate group bots** so the data report and competitor
intelligence land in different chats.

1. Open the target Lark group → **Settings → Bots → Add Bot → Custom Bot**.
2. Name it (e.g. `数据周报机器人`) and copy the **Webhook URL**
   (`https://open.larksuite.com/open-apis/bot/v2/hook/...`).
3. Repeat in the second group for the competitor bot.
4. Put the two URLs into `environment.env`:
   ```dotenv
   LARK_WEBHOOK_WEEKLY=...       # data report bot
   LARK_WEBHOOK_COMPETITOR=...   # competitor bot
   ```
5. (Optional) For richer security, enable **signature verification** on the bot
   — not required in this implementation, which posts unsigned JSON.

**Card format** (`lark_notifier.send_card`): an interactive card with a
coloured title header, a Markdown body, and one or more link buttons
(`查看完整报告`) pointing at the HTML report. A run-level success/failure text
message is also sent.

Each run additionally fires a **macOS notification** with:
`一句话结论 · 运行状态 · 结束运行时间 · 邮件推送状态 · Lark推送状态`.

---

## 5. Gmail setup (OAuth)

1. In Google Cloud Console create a project and enable the **Gmail API**.
2. Create an **OAuth client ID → Desktop app**, then download the JSON to
   `credentials/client_secret.json`.
3. Set `GMAIL_SENDER` and `GMAIL_RECIPIENTS` in `environment.env`
   (`GMAIL_RECIPIENTS` accepts a comma-separated list — your stakeholder group).
4. The **first run opens a browser** to grant consent and writes
   `credentials/token.json`. Afterwards the stored refresh token is reused
   silently, which is what makes scheduled/server execution possible.

> On a headless server: run step 4 once on your laptop, then copy
> `credentials/token.json` to the server.


---

## 6. Scheduling & automation

### 6.1 Cline Schedule (recommended for local/desktop)
Create two scheduled runs:

| Job | Cron / time | Command |
|---|---|---|
| Data report | every day 09:00 | `python -m src.pipeline report --type auto` |
| Competitor monitor | every day 09:00 | `python -m src.pipeline competitor` |

The report job is frequency-aware: run daily and it produces the **daily**
report every day, the **weekly** report automatically on Mondays (09:00) and the
**monthly** report on the 1st. If you only ever run on Mondays, set it to
`--type weekly`; for a full periodic sweep use `all`.

### 6.2 cron (on a server)
```cron
0 9 * * *  cd /opt/report_competitor_monitor && /opt/conda/envs/report_competitor_monitor/bin/python -m src.pipeline report --type auto  >> logs/report.log 2>&1
0 9 * * *  cd /opt/report_competitor_monitor && /opt/conda/envs/report_competitor_monitor/bin/python -m src.pipeline competitor        >> logs/competitor.log 2>&1
```

Every run:
* generates the HTML report(s) into `reports/`,
* pushes a Lark card + a run status message,
* sends the HTML email (cadence-dependent; competitor always, separately),
* fires a macOS notification with the 5-field summary,
* writes a row to the `run_log` table.

---

## 7. Docker

```bash
# build
docker compose build

# first-run bootstrap (all three reports + competitor)
docker compose --profile bootstrap up bootstrap

# normal single runs
docker compose up report-pipeline
docker compose up competitor-monitor
```

`docker-compose.yml` defines three services sharing `./reports`, `./data` and
`./credentials` as volumes:
* `report-pipeline` → `python -m src.pipeline report --type auto`
* `competitor-monitor` → `python -m src.pipeline competitor`
* `bootstrap` (profile `bootstrap`) → `python -m src.pipeline all`

To run recurring inside Docker, add a cron sidecar or schedule
`docker compose up` from the host crontab.


---

## 8. Production deployment (company cloud)

This repository runs fine on a laptop, but a data service that must be reliable
**should not run on your local machine**. For a company production environment,
deploy it to a cloud server — **Alibaba Cloud (ECS) / Tencent Cloud (CVM)** or
any always-on Linux host — and let it run on a schedule there:

1. **Provision a small always-on instance** (1–2 vCPU is plenty; the workload is
   mostly network I/O and a little LLM inference).
2. **Install the runtime**: Docker + docker compose (simplest), or Miniconda with
   the `report_competitor_monitor` env.
3. **Clone the repo** and create `environment.env` with the production secrets.
   Store secrets in the cloud provider's secret manager / KMS where possible.
4. **Serve the reports**: point a reverse proxy (Nginx) at the `reports/`
   directory and set `REPORT_BASE_URL=https://reports.your-company.com` so the
   Lark card buttons open the live HTML in any browser.
5. **Schedule it** with `cron` or a systemd timer (see §6.2), or run the
   `docker-compose` services from cron.
6. **Configure the stakeholder email group**: put your stakeholders' addresses
   (or a Google Group distribution list) in `GMAIL_RECIPIENTS` so every report
   reaches the whole group:
   ```dotenv
   GMAIL_RECIPIENTS=data-team@your-company.com,leadership@your-company.com
   ```
7. **Grant Gmail consent once** (copy `credentials/token.json` from a machine
   with a browser), then the server refreshes tokens automatically.

With steps 1–7 in place the project becomes a **self-running cloud data service**
that keeps publishing the daily/weekly/monthly data reports and the daily
competitor report — independent of whether your laptop is on.

---

## 9. Configuration reference

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` / `_BASE_URL` / `_MODEL` | LLM access |
| `LLM_TEMPERATURE_REPORT` / `_ANALYZER` | narrative (0.6) vs classification (0.1) |
| `LARK_WEBHOOK_WEEKLY` / `_COMPETITOR` | the two bot webhooks |
| `GMAIL_CLIENT_SECRET_FILE` / `_TOKEN_FILE` | OAuth files |
| `GMAIL_SENDER` / `GMAIL_RECIPIENTS` | sender + stakeholder group |
| `REPORTS_DIR` / `DB_PATH` / `REPORT_BASE_URL` | outputs + report link base |
| `ENABLE_MAC_NOTIFY` / `LARK_ENABLED` / `GMAIL_ENABLED` | per-channel toggles |

---

## 10. Notes

* External announcement endpoints change frequently; adapters degrade to a
  seeded sample set so a report is always produced, and log a warning when a
  source is unreachable.
* Internal KPIs (VIP tiering, Earn inflow, registrations) are **modelled** with
  a deterministic generator — replace `weekly_data_fetcher._simulated_internal`
  with real queries when connecting to production data.
* See [`docs/development-log.md`](docs/development-log.md) for design rationale
  and [`docs/insights.md`](docs/insights.md) for evolving business insights.

```
