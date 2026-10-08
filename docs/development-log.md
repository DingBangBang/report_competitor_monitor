# Development Log

Technical decisions behind the **Automated Data Reports & Competitor Monitoring System**.

---

## 1. Why two independent pipelines?

The data report and the competitor monitor have different rhythms, data sources
and stakeholders:

| | Data report | Competitor monitor |
|---|---|---|
| Cadence | daily / weekly / monthly | daily |
| Source | Binance market API + internal KPIs | Binance/OKX/Bybit announcements |
| Bot | `LARK_WEBHOOK_WEEKLY` | `LARK_WEBHOOK_COMPETITOR` |
| Email | one combined email per cadence | its own standalone daily email |

Decoupling them means a failure in scraping does not block the report and vice
versa. They share only the low-level modules (`database`, `llm_client`,
`lark_notifier`, `email_notifier`).

## 2. Why DeepSeek?

* **Cost/latency**: DeepSeek-V3 (`deepseek-chat`) offers strong Chinese
  generation at a fraction of the cost of frontier models, which matters for a
  daily job that runs thousands of times a year.
* **OpenAI-compatible API**: the `llm_client` is a thin wrapper over the
  `/chat/completions` endpoint, so swapping to another provider is a one-line
  base-URL/model change.
* **Native JSON mode**: `response_format: json_object` plus the schema prompts
  below make outputs machine-parseable.

### Graceful degradation
Every LLM call is optional. If the key is missing or the call fails after
retries, generators fall back to deterministic, data-driven narratives and the
rule-based impact classifier. **The pipeline never produces an empty report.**

## 3. Schema Prompt design

Free-form prompting produced unparseable output ("结论" buried at the end,
hallucinated numbers). We therefore constrain the model with a **strict JSON
schema** and explicit rules:

### Report narrative (`weekly_report_generator.py`)
```json
{
  "headline": "one-sentence conclusion with >=2 concrete numbers",
  "core_metrics": "markdown using '$XX亿, +X%' format",
  "anomalies": ["..."],
  "hypotheses": "...",
  "next_steps": ["..."]
}
```
Rules baked into the system prompt:
1. **Conclusion first** — `headline` must lead with the punchline.
2. **Mandatory numbers** — every claim must cite a value from the provided data;
   fabricating numbers is explicitly forbidden.
3. **Fixed sections** — core metrics → anomalies → hypotheses → next steps.

### Impact classification (`competitor_analyzer.py`)
Low temperature (**0.1**) because classification must be stable: the same event
must not flip between `immediate` and `monitor` on consecutive runs. Output:
```json
{ "impact_level": "...", "impact_reason": "...",
  "suggested_action": "...", "urgency_score": 1 }
```

`LLMClient._safe_json` tolerates code fences and stray prose by extracting the
outermost `{...}` block, so a slightly chatty model does not break parsing.

## 4. Impact-priority classification logic

| Level | Meaning | Trigger (rules / typical LLM reasoning) |
|---|---|---|
| `immediate` | 立即响应 | New listings & fee changes — directly affect our users and liquidity. |
| `confirm` | 进一步确认 | Delistings & ambiguous events — verify scope, look for migration opportunities. |
| `management_align` | 管理层对齐 | Cross-department coordination or strategic shifts. |
| `monitor` | 持续观察 | Campaigns / low-relevance events — log only. |

The rule-based fallback mirrors this table so behaviour is consistent whether or
not the LLM is reachable.

## 5. Data model & deduplication

* `event_id = sha256(exchange|title|published_at)[:24]` — stable and unique
  enough to dedupe across runs while allowing a re-published title on a new date
  to be counted again.
* Incremental scraping uses `fetch_state.last_fetch_at`: only announcements
  published after the last successful run are inserted.
* `weekly_metrics` is keyed by `(report_type, metric_key, period_end)` and
  upserted, so re-running on the same day simply refreshes the row.

## 6. Notification fan-out

Each run reports through **three channels**:

1. **Lark** — a rich card (metrics/top-3 + "view full report" button) plus a
   run-level success/failure text.
2. **Gmail** — the full HTML report (competitor report is a *separate* email).
3. **macOS** — a `terminal-notifier`/`osascript` toast with the required five
   fields: one-line conclusion, run status, finish time, email status, Lark
   status.

All three are best-effort: a missing webhook or credential downgrades the status
string but never raises.

## 7. Internal KPI modelling

Binance exposes market data but not internal KPIs (VIP tiering, Earn inflows,
registrations). `weekly_data_fetcher._simulated_internal` generates them with a
**period-seeded** `random.Random` so values are stable for a given period and
reproducible. Swapping in real SQL is a single-method change.

## 8. Calendar-aligned reporting windows

Rolling windows made the Monday report cover "the last 7 days ending today",
which is not what "last week" means to stakeholders. `_period_range()` now
aligns to **completed calendar periods**:

| Report | Window | Rationale |
|---|---|---|
| daily | yesterday | a full day of data |
| weekly | last completed Mon–Sun | Monday's run reports the week that just closed |
| monthly | previous calendar month | the 1st-of-month run reports last month |

Any `--type` other than `auto` is treated as a **manual trigger** and forces
generation + push, so reports can be tested at any time.

## 9. Anti-duplication design (report content)

The first LLM version repeated the same numbers and near-identical sentences
across four blocks (headline → cards → executive summary → dimension insights →
tables). Three changes removed it:

1. **Separate data from interpretation.** Cards + tables carry the *numbers*;
   the narrative carries only *judgement*. The `executive_summary` block was
   deleted as pure restatement.
2. **Consume each metric once.** In the fallback narrator, metrics are ranked by
   |change| and each narrative field claims a *distinct* metric via a `used` set,
   so no data point is cited twice. The LLM prompt now forbids repetition and
   cross-field reuse of a value; `_dedupe_narrative()` additionally drops any
   highlight/anomaly whose value token already appeared in the headline.
3. **No summary/detail overlap.** The dimension tables exclude the six metrics
   already shown in the cards (annotated "核心指标见上方卡片"), so every number
   appears exactly once in the report body.

The competitor report applies the same principle: `top_three` is now a concise
one-line judgement (≤30 chars, not the raw announcement title), and the bottom
provenance table lists exchange/category/time/URL without repeating titles.

