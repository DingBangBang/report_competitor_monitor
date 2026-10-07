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

SCHEMA_INSTRUCTION = """你是资深数据分析师。请严格基于提供的数据，输出**唯一一个 JSON 对象**，不要输出任何多余文字或代码块标记。
JSON schema：
{
  "headline": "一句话核心结论，必须包含至少两个具体数值与同比/环比方向",
  "core_metrics": "核心指标段落（markdown，可用短横线列表），必须引用具体数值，格式如：本周合约交易量 $XX亿，较上周增长X%",
  "anomalies": ["异常点1（含数值与偏离幅度）", "异常点2"],
  "hypotheses": "针对异常点的可能原因假设（markdown 段落）",
  "next_steps": ["建议动作1", "建议动作2", "建议动作3"]
}
要求：结论前置；所有数字必须来自给定数据，不得编造；语气客观专业。"""


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

        narrative = self._generate_narrative(report_type, metrics)

        context = {
            "title": f"数据{REPORT_LABELS.get(report_type, '报告')}",
            "report_type": report_type,
            "period_start": period_start,
            "period_end": period_end,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "metrics": metrics,
            "headline": narrative.get("headline", ""),
            "core_metrics_md": narrative.get("core_metrics", ""),
            "anomalies": narrative.get("anomalies", []),
            "hypotheses": narrative.get("hypotheses", ""),
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

    # -- narrative -----------------------------------------------------------
    def _generate_narrative(self, report_type: str, metrics: list[dict[str, Any]]) -> dict[str, Any]:
        if not metrics:
            return {
                "headline": "本期暂无可用数据。",
                "core_metrics": "未采集到指标数据。",
                "anomalies": [],
                "hypotheses": "",
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
                parsed.setdefault("core_metrics", "")
                parsed.setdefault("anomalies", [])
                parsed.setdefault("hypotheses", "")
                parsed.setdefault("next_steps", [])
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
                f"- {m['metric_label']}({m['metric_key']}): {m['metric_value']}{m.get('metric_unit','')} "
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

        headline = (
            f"本{label}合约交易量与现货交投活跃，"
            f"{fmt('contract_volume')}；{fmt('spot_volume')}。"
        )
        core = "\n".join(
            f"- {fmt(k)}"
            for k in ("contract_volume", "spot_volume", "active_pairs", "fund_net_flow",
                      "vip_user_change", "earn_net_inflow", "new_registrations")
            if k in by_key
        )

        anomalies = []
        for m in metrics:
            chg = m.get("change_pct")
            if chg is not None and abs(chg) >= 15:
                anomalies.append(
                    f"{m['metric_label']}出现显著波动（{chg:+.1f}%），当前值 {m['metric_value']}{m.get('metric_unit','')}。"
                )

        hypotheses = (
            "上述波动可能与宏观行情震荡、竞品近期上币/费率调整引发的资金迁移，"
            "以及季节性活动投放相关；建议结合竞品动态报告交叉验证。"
        )
        next_steps = [
            "对异常指标下钻到币种/用户分层维度定位原因。",
            "关注竞品上币与费率变化对 VIP 用户留存的影响。",
            "评估并优化 Earn 产品收益率以稳定净流入。",
        ]
        return {
            "headline": headline,
            "core_metrics": core,
            "anomalies": anomalies,
            "hypotheses": hypotheses,
            "next_steps": next_steps,
        }
