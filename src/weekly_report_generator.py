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
JSON schema：
{
  "headline": "一句话核心结论，包含至少两个具体数值与环比方向",
  "executive_summary": "执行摘要（3-5句，markdown），覆盖交易/资金/用户/市场四个维度的总体判断",
  "dimension_insights": [
    {"dimension": "交易维度", "insight": "该维度一句话解读，引用具体数值与环比"},
    {"dimension": "资金维度", "insight": "..."},
    {"dimension": "用户维度", "insight": "..."},
    {"dimension": "市场维度", "insight": "..."}
  ],
  "anomalies": ["异常点（含数值与偏离幅度，如：Earn净流入环比 -32%，显著低于近月均值）"],
  "hypotheses": "针对异常点的可能原因假设（markdown 段落，分点）",
  "risks": ["潜在风险1", "潜在风险2"],
  "next_steps": ["建议动作1", "建议动作2", "建议动作3", "建议动作4"]
}
要求：结论前置；四个维度都要覆盖；所有数字必须来自给定数据，不得编造；语气客观专业。"""


class WeeklyReportGenerator:
    """Turns stored KPI rows into a rendered HTML report."""

    def __init__(self, db: Database | None = None, llm: LLMClient | None = None) -> None:
        self.db = db or Database()
        self.llm = llm or LLMClient()
        self.env = Environment(
            loader=FileSystemLoader(str(Config.TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )

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
        metrics_by_dim, dimension_metrics = self._group_by_dimension(metrics)
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
            "cards": self._card_metrics(metrics),
            "gainers": extras.get("top_gainers", []),
            "losers": extras.get("top_losers", []),
            "headline": narrative.get("headline", ""),
            "executive_summary": narrative.get("executive_summary", ""),
            "dimension_insights": narrative.get("dimension_insights", []),
            "anomalies": narrative.get("anomalies", []),
            "hypotheses": narrative.get("hypotheses", ""),
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
                "executive_summary": "未采集到指标数据。",
                "dimension_insights": [],
                "anomalies": [],
                "hypotheses": "",
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
                    ("executive_summary", ""), ("dimension_insights", []),
                    ("anomalies", []), ("hypotheses", ""), ("risks", []), ("next_steps", []),
                ):
                    parsed.setdefault(k, default)
                return parsed
            logger.warning("LLM narrative unavailable/invalid; using deterministic fallback.")

        return self._fallback_narrative(report_type, metrics)

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

        def fmt(key: str) -> str:
            m = by_key.get(key)
            if not m:
                return "暂无数据"
            chg = m.get("change_pct")
            direction = "增长" if (chg or 0) >= 0 else "下降"
            chg_txt = f"较上期{direction}{abs(chg):.1f}%" if chg is not None else "无环比数据"
            return f"{m['metric_label']} {m['metric_value']}{m.get('metric_unit','')}，{chg_txt}"

        def val(key: str, default: str = "N/A") -> str:
            m = by_key.get(key)
            return f"{m['metric_value']}{m.get('metric_unit','')}" if m else default

        headline = (
            f"本{label}合约交易量 {val('contract_volume')}（{_chg_txt(by_key.get('contract_volume'))}），"
            f"现货交易量 {val('spot_volume')}（{_chg_txt(by_key.get('spot_volume'))}），"
            f"资金净流入 {val('fund_net_flow')}（{_chg_txt(by_key.get('fund_net_flow'))}）。"
        )

        # Executive summary across the four dimensions.
        exec_parts = [
            f"交易端：{fmt('contract_volume')}；{fmt('spot_volume')}；活跃交易对 {by_key.get('active_pairs',{}).get('metric_value','N/A')} 个。",
            f"资金端：净流入 {by_key.get('fund_net_flow',{}).get('metric_value','N/A')}$亿，Earn 净流入 {by_key.get('earn_net_inflow',{}).get('metric_value','N/A')}$M，稳定币储备 {by_key.get('stablecoin_reserve',{}).get('metric_value','N/A')}$亿。",
            f"用户端：新增注册 {by_key.get('new_registrations',{}).get('metric_value','N/A')} 人，日活 {by_key.get('active_users',{}).get('metric_value','N/A')} 万人，VIP 净变动 {by_key.get('vip_user_change',{}).get('metric_value','N/A')} 人。",
            f"市场端：BTC {by_key.get('btc_price',{}).get('metric_value','N/A')}$（{_chg_txt(by_key.get('btc_change_pct'))}），上涨币种占比 {by_key.get('market_breadth',{}).get('metric_value','N/A')}%。",
        ]

        # Per-dimension one-line insights.
        dimension_insights = []
        for dim in DIMENSION_ORDER:
            items = [m for m in metrics if (m.get("dimension") or "other") == dim]
            if not items:
                continue
            # pick the metric with the largest absolute change for the headline of the dimension.
            lead = max(items, key=lambda m: abs(m.get("change_pct") or 0))
            dimension_insights.append({
                "dimension": DIMENSION_LABELS.get(dim, dim),
                "insight": f"{DIMENSION_LABELS.get(dim, dim)}：主线指标 {fmt(lead['metric_key'])}；"
                           f"波动最大项为 {lead['metric_label']}（{_chg_txt(lead)}）。",
            })

        anomalies = []
        for m in metrics:
            chg = m.get("change_pct")
            if chg is not None and abs(chg) >= 15:
                anomalies.append(
                    f"{m['metric_label']}出现显著波动（{chg:+.1f}%），当前值 {m['metric_value']}{m.get('metric_unit','')}。"
                )
        if not anomalies:
            anomalies.append("本期各核心指标波动均在 ±15% 的正常区间内，无显著异常。")

        hypotheses = (
            "可能原因假设：\n"
            "- 交易量波动多与大盘行情及衍生品资金费率变化相关；\n"
            "- 资金净流入/流出与 Earn 收益率、竞品上币及费率调整存在相关性；\n"
            "- 用户指标变化受活动投放节奏与竞品挖矿活动分流影响。\n"
            "建议结合竞品动态报告交叉验证。"
        )

        risks = [
            "若资金净流入持续为负，需警惕流动性外流与做市深度下降。",
            "竞品上币/费率调整可能持续分流 VIP 与高净值用户。",
            "稳定币储备若下降，将削弱大额交易的承接能力。",
        ]
        next_steps = [
            "对波动最大的 2-3 个指标下钻到币种/用户分层维度定位原因。",
            "跟踪竞品本周上币与费率变化，评估我方跟进策略。",
            "优化 Earn 产品收益率与 VIP 权益以稳定净流入。",
            "监控稳定币储备与做市深度，必要时启动流动性补充预案。",
        ]
        return {
            "headline": headline,
            "executive_summary": "\n".join(exec_parts),
            "dimension_insights": dimension_insights,
            "anomalies": anomalies,
            "hypotheses": hypotheses,
            "risks": risks,
            "next_steps": next_steps,
        }


def _chg_txt(m: dict[str, Any] | None) -> str:
    if not m:
        return "无环比数据"
    chg = m.get("change_pct")
    if chg is None:
        return "无环比数据"
    return f"较上期{'增长' if chg >= 0 else '下降'}{abs(chg):.1f}%"
