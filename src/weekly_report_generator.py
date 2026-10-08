"""Generate the daily / weekly / monthly data report text and HTML.

The narrative is produced by DeepSeek using a *schema prompt* that forces a
strict JSON shape (see ``SCHEMA_INSTRUCTION``).  When the LLM is unavailable the
module falls back to a deterministic, data-driven narrative so the report is
never empty.  The final HTML is rendered from ``templates/weekly_report.html``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import Config
from src.database import Database
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

REPORT_LABELS = {"daily": "日报", "weekly": "周报", "monthly": "月报"}

DIMENSION_ORDER = ["trading", "funds", "users", "market"]
DIMENSION_LABELS = {
    "trading": "交易维度",
    "funds": "资金维度",
    "users": "用户维度",
    "market": "市场维度",
}

SCHEMA_INSTRUCTION = """你是资深加密交易所数据分析师。请严格基于给定数据，输出**唯一一个 JSON 对象**，不要任何多余文字或代码块标记。

你的任务是「解读」而非「复述」。报告正文已经用指标卡片和表格完整列出了所有数值，因此必须遵守：
1. 严禁在正文中重新罗列指标清单，严禁重复卡片/表格里已出现的数值堆砌；
2. 每个字段只承载一类信息，字段之间不得重复同一句话或同一个数据点；
3. 必须引用数值时要服务于判断，且同一个数值全篇最多出现一次；
4. 不要写「本报告将分析…」「综上所述」这类空话与套话，直接给判断；
5. headline 与 dimension_highlights、anomalies 之间不得引用同一个指标或同一个数值。

JSON schema：
{
  "headline": "一句话核心结论（≤60字）：本期最重要的变化及其业务含义",
  "dimension_highlights": [
    {"dimension": "交易维度", "highlight": "该维度的一句话解读：变化 + 驱动/含义（≤50字）"},
    {"dimension": "资金维度", "highlight": "..."},
    {"dimension": "用户维度", "highlight": "..."},
    {"dimension": "市场维度", "highlight": "..."}
  ],
  "anomalies": ["只列真实显著异常（环比绝对值≥15%），每条一句话；没有异常就返回空数组 []"],
  "analysis": "原因分析（markdown，2-4 段）：把变化串成因果链，说明可能的驱动因素与相互影响，不要罗列数字",
  "risks": ["具体风险，每条一句话，不得与 analysis 内容重复"],
  "next_steps": ["可执行的下一步动作，每条一句话，具体到做什么"]
}
四个维度都要覆盖；所有数字必须来自给定数据，不得编造。"""


class WeeklyReportGenerator:
    """Turns stored KPI rows into a rendered HTML report."""

    def __init__(self, db: Database | None = None, llm: LLMClient | None = None) -> None:
        self.db = db or Database()
        self.llm = llm or LLMClient()
        self.env = Environment(
            loader=FileSystemLoader(str(Config.TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        # Render 56600.0 as 56600 while keeping genuine decimals intact.
        self.env.filters["num"] = _fmt_num
    # -- public API ----------------------------------------------------------
    def generate(self, report_type: str = "weekly") -> dict[str, Any]:
        report_type = report_type.lower()
        metrics = self.db.get_metrics(report_type)
        if not metrics:
            period_end = self.db.latest_period_end(report_type)
            metrics = self.db.get_metrics(report_type, period_end)

        period_start = metrics[0]["period_start"] if metrics else ""
        period_end = metrics[0]["period_end"] if metrics else datetime.now(timezone.utc).date().isoformat()

        extras = self.db.get_extras(report_type, period_end) if period_end else {}
        cards = self._card_metrics(metrics)
        card_keys = {m["metric_key"] for m in cards}
        # The tables carry the *remaining* metrics so numbers are never shown twice.
        rest = [m for m in metrics if m["metric_key"] not in card_keys]
        metrics_by_dim, dimension_metrics = self._group_by_dimension(rest)
        narrative = self._generate_narrative(report_type, metrics)

        context = {
            "title": f"数据{REPORT_LABELS.get(report_type, '报告')}",
            "report_type": report_type,
            "period_start": period_start,
            "period_end": period_end,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "metrics": metrics,
            "metrics_by_dim": metrics_by_dim,
            "dimensions": DIMENSION_ORDER,
            "dimension_labels": DIMENSION_LABELS,
            "cards": cards,
            "gainers": extras.get("top_gainers", []),
            "losers": extras.get("top_losers", []),
            "headline": narrative.get("headline", ""),
            "dimension_highlights": narrative.get("dimension_highlights", []),
            "anomalies": narrative.get("anomalies", []),
            "analysis": narrative.get("analysis", ""),
            "risks": narrative.get("risks", []),
            "next_steps": narrative.get("next_steps", []),
        }

        html = self.env.get_template("weekly_report.html").render(**context)
        filename = f"{report_type}_report_{period_end}.html"
        out_path = Path(Config.REPORTS_DIR) / filename
        out_path.write_text(html, encoding="utf-8")
        logger.info("Wrote report -> %s", out_path)

        context["html_path"] = str(out_path)
        context["filename"] = filename
        context["narrative"] = narrative
        return context

    @staticmethod
    def _group_by_dimension(metrics: list[dict[str, Any]]) -> tuple[dict[str, list], list[str]]:
        by_dim: dict[str, list] = {d: [] for d in DIMENSION_ORDER}
        for m in metrics:
            by_dim.setdefault(m.get("dimension") or "other", []).append(m)
        present = [d for d in DIMENSION_ORDER if by_dim.get(d)]
        return by_dim, present

    @staticmethod
    def _card_metrics(metrics: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
        priority = [
            "contract_volume", "spot_volume", "active_pairs", "fund_net_flow",
            "new_registrations", "active_users", "earn_net_inflow", "btc_price",
        ]
        by_key = {m["metric_key"]: m for m in metrics}
        cards = [by_key[k] for k in priority if k in by_key]
        return cards[:limit]

    # -- narrative -----------------------------------------------------------
    def _generate_narrative(self, report_type: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
        if not metrics:
            return {
                "headline": "本期暂无可用数据。",
                "dimension_highlights": [],
                "anomalies": [],
                "analysis": "",
                "risks": [],
                "next_steps": ["检查数据抓取管道是否正常运行。"],
            }

        data_block = self._metrics_to_text(report_type, metrics)
        if self.llm.available:
            reply = self.llm.chat(
                system_prompt=SCHEMA_INSTRUCTION,
                user_prompt=f"报告类型：{REPORT_LABELS.get(report_type, report_type)}\n指标数据：\n{data_block}",
                temperature=Config.LLM_TEMPERATURE_REPORT,
                json_mode=True,
            )
            parsed = LLMClient._safe_json(reply) if reply else None
            if parsed and parsed.get("headline"):
                for k, default in (
                    ("dimension_highlights", []), ("anomalies", []),
                    ("analysis", ""), ("risks", []), ("next_steps", []),
                ):
                    parsed.setdefault(k, default)
                return self._dedupe_narrative(parsed)
            logger.warning("LLM narrative unavailable/invalid; using deterministic fallback.")

        return self._fallback_narrative(report_type, metrics)

    @staticmethod
    def _dedupe_narrative(n: dict[str, Any]) -> dict[str, Any]:
        """Guard against any residual repetition in the model's output."""
        import re

        def _values(text: Any) -> set[str]:
            """Value-like numeric tokens (decimals or >=100), skipping labels like '7日'."""
            return set(re.findall(r"\d+\.\d+|\d{3,}", str(text)))

        seen: set[str] = set()

        def unique_list(items: Any) -> list[str]:
            out: list[str] = []
            for it in items or []:
                text = it if isinstance(it, str) else (it.get("highlight") or it.get("insight") or "")
                key = " ".join(str(text).split()).lower()
                if text and key not in seen:
                    seen.add(key)
                    out.append(it)
            return out

        headline_vals = _values(n.get("headline", ""))

        def not_in_headline(items: Any) -> list[str]:
            out: list[str] = []
            for it in items or []:
                text = it if isinstance(it, str) else (it.get("highlight") or it.get("insight") or "")
                if _values(text) & headline_vals:
                    continue  # already quantified in the headline
                out.append(it)
            return out

        n["anomalies"] = not_in_headline(unique_list(n.get("anomalies")))
        n["risks"] = unique_list(n.get("risks"))
        n["next_steps"] = unique_list(n.get("next_steps"))

        # A dimension highlight must not repeat the headline's figures, nor an anomaly line.
        anomaly_keys = {" ".join(str(a).split()).lower() for a in n["anomalies"]}
        n["dimension_highlights"] = [
            d for d in not_in_headline(n.get("dimension_highlights") or [])
            if " ".join(str(d.get("highlight", "")).split()).lower() not in anomaly_keys
        ]
        return n

    @staticmethod
    def _metrics_to_text(report_type: str, metrics: list[dict[str, Any]]) -> str:
        lines = []
        for m in metrics:
            change = m.get("change_pct")
            change_txt = f"{change:+.1f}%" if change is not None else "N/A"
            lines.append(
                f"- [{DIMENSION_LABELS.get(m.get('dimension',''), m.get('dimension',''))}] "
                f"{m['metric_label']}({m['metric_key']}): {m['metric_value']}{m.get('metric_unit','')} "
                f"[较上期 {change_txt}] 来源:{m.get('source','')}"
            )
        return f"报告口径: {REPORT_LABELS.get(report_type, report_type)}\n" + "\n".join(lines)

    def _fallback_narrative(self, report_type: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
        label = REPORT_LABELS.get(report_type, "本期")
        by_key = {m["metric_key"]: m for m in metrics}

        # Rank every metric once; each narrative field then consumes a *distinct*
        # metric so the same data point never appears twice in the prose.
        ranked = sorted(metrics, key=lambda m: abs(m.get("change_pct") or 0), reverse=True)
        used: set[str] = set()

        # 1) Headline — the single most significant mover this period.
        lead = ranked[0] if ranked else None
        if lead:
            used.add(lead["metric_key"])
            flow = _val(by_key, "fund_net_flow")
            flow_txt = "资金面呈净流入" if (flow or 0) >= 0 else "资金面呈净流出"
            headline = f"{lead['metric_label']}环比{_chg_txt(lead)}，为{label}最显著变化，{flow_txt}。"
        else:
            headline = f"本{label}暂无可用数据。"

        # 2) Anomalies — genuine outliers only, excluding what the headline used.
        anomalies: list[str] = []
        for m in ranked:
            chg = m.get("change_pct")
            if chg is not None and abs(chg) >= 15 and m["metric_key"] not in used:
                used.add(m["metric_key"])
                anomalies.append(f"{m['metric_label']}偏离显著（{chg:+.1f}%），需重点关注。")
            if len(anomalies) >= 4:
                break

        # 3) Per-dimension highlight — top mover *within* each dimension, still
        #    excluding anything already mentioned above.
        dimension_highlights: list[dict[str, str]] = []
        for dim in DIMENSION_ORDER:
            items = [
                m for m in metrics
                if (m.get("dimension") or "other") == dim and m["metric_key"] not in used
            ]
            if not items:
                continue
            top = max(items, key=lambda m: abs(m.get("change_pct") or 0))
            used.add(top["metric_key"])
            direction = "上行" if (top.get("change_pct") or 0) >= 0 else "回落"
            dimension_highlights.append({
                "dimension": DIMENSION_LABELS.get(dim, dim),
                "highlight": f"{DIMENSION_LABELS.get(dim, dim)}{direction}，主要由{top['metric_label']}带动。",
            })

        breadth = _val(by_key, "market_breadth")
        breadth_txt = (
            "市场上涨币种占比偏低，资金集中在少数主流币种，行情广度不足。"
            if (breadth or 100) < 45
            else "市场上涨币种占比较高，行情具备一定广度。"
        )
        analysis = (
            "本期的核心变化集中在交易与资金两条主线：交易量的变动通常先由大盘行情与衍生品资金费率"
            "驱动，再传导至现货深度；资金面方向与交易活跃度方向的一致性，说明驱动力更多来自资金迁移"
            "而非单纯的价格波动。" + breadth_txt +
            "用户侧的新增与留存属于滞后指标，其变化需要在后续周期中确认，暂不足以判断趋势拐点。"
        )

        risks = [
            "资金面若持续净流出，可能削弱做市深度并推高用户交易成本。",
            "竞品在上币与费率上的主动调整，可能持续分流高净值与 VIP 用户。",
            "市场广度不足时，行情依赖少数主流币种，波动放大风险上升。",
        ]
        next_steps = [
            "对资金面异动下钻到币种与做市商维度，定位净流出的具体来源。",
            "跟踪竞品本期的上币与费率变化，评估是否需要跟进调整。",
            "优化 Earn 收益率与 VIP 权益结构，稳定高净值用户留存。",
            "在行情广度收窄时加强流动性监控，必要时启动做市补充预案。",
        ]
        return {
            "headline": headline,
            "dimension_highlights": dimension_highlights,
            "anomalies": anomalies,
            "analysis": analysis,
            "risks": risks,
            "next_steps": next_steps,
        }


def _val(by_key: dict[str, Any], key: str) -> float | None:
    m = by_key.get(key)
    return m.get("metric_value") if m else None


def _fmt_value(m: dict[str, Any] | None) -> str:
    if not m:
        return ""
    return _fmt_num(m.get("metric_value"))


def _fmt_num(v: Any) -> str:
    """Format a raw number: 56600.0 -> "56600", 4225.2 -> "4225.2"."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _chg_txt(m: dict[str, Any] | None) -> str:
    if not m:
        return "无环比数据"
    chg = m.get("change_pct")
    if chg is None:
        return "无环比数据"
    return f"较上期{'增长' if chg >= 0 else '下降'}{abs(chg):.1f}%"
