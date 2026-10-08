"""Assemble the competitor intelligence report (HTML) from analysed events.

Structure: conclusion-first ("本周最需要关注的三件事"), then events grouped by
impact priority (immediate -> confirm -> management_align -> monitor), then the
analytical appendix (overview / impact logic / historical comparison).  Each
event carries its source URL and timestamp.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.analyzer_labels import CATEGORY_LABELS, GROUP_ORDER, LEVEL_LABELS
from src.competitor_fetcher import SYMBOL_STOPWORDS
from src.config import Config
from src.database import Database
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

SCHEMA_INSTRUCTION = """你是加密交易所竞品情报主管。基于给定的竞品事件列表，输出**唯一一个 JSON 对象**：
{
  "top_three": [
    "第1件：≤30字，点明交易所 + 事件 + 为什么重要，禁止直接照抄公告标题",
    "第2件：同样风格", "第3件：同样风格"
  ],
  "overview": "本周事件概览（markdown 段落，1-2 段，按交易所/类型归纳趋势，不要逐条复述下方列表）",
  "impact_logic": "影响评估逻辑说明（markdown 段落，说明分类依据）",
  "history_compare": "与历史的对比（markdown 段落，若无历史数据请说明）"
}
要求：结论前置；top_three 恰好三条且互不重复；所有内容基于给定事件，不得编造；禁止与报告下方的事件明细逐字重复。"""


class CompetitorReportGenerator:
    """Renders ``templates/competitor_report.html`` from stored events."""

    def __init__(self, db: Database | None = None, llm: LLMClient | None = None) -> None:
        self.db = db or Database()
        self.llm = llm or LLMClient()
        self.env = Environment(
            loader=FileSystemLoader(str(Config.TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def generate(self, days: int = 7) -> dict[str, Any]:
        events = self.db.get_recent_events(days=days)
        today = datetime.now(timezone.utc).date().isoformat()
        period_start = events[-1]["published_at"][:10] if events else today

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in events:
            groups[ev.get("impact_level") or "monitor"].append(ev)

        narrative = self._generate_narrative(events, groups)
        keywords = self._extract_keywords(events)

        context = {
            "title": "竞品动态监控报告",
            "period_start": period_start,
            "period_end": today,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "total_events": len(events),
            "keywords": keywords,
            "top_three": narrative.get("top_three", []),
            "groups": {lvl: groups.get(lvl, []) for lvl in GROUP_ORDER},
            "level_labels": LEVEL_LABELS,
            "overview": narrative.get("overview", ""),
            "impact_logic": narrative.get("impact_logic", ""),
            "history_compare": narrative.get("history_compare", ""),
            "all_sources": events,
        }

        html = self.env.get_template("competitor_report.html").render(**context)
        filename = f"competitor_report_{today}.html"
        out_path = Path(Config.REPORTS_DIR) / filename
        out_path.write_text(html, encoding="utf-8")
        logger.info("Wrote competitor report -> %s", out_path)

        context["html_path"] = str(out_path)
        context["filename"] = filename
        context["narrative"] = narrative
        return context

    # -- keywords ------------------------------------------------------------
    @staticmethod
    def _extract_keywords(events: list[dict[str, Any]], top_n: int = 12) -> dict[str, Any]:
        """Summarise the reporting window into a set of headline keywords."""
        symbol_counts: Counter = Counter()
        category_counts: Counter = Counter()
        exchange_counts: Counter = Counter()

        for ev in events:
            for sym in ev.get("symbols") or []:
                if 2 <= len(sym) <= 10 and sym not in SYMBOL_STOPWORDS:
                    symbol_counts[sym] += 1
            category_counts[ev.get("category", "activity")] += 1
            exchange_counts[ev.get("exchange", "Unknown")] += 1

        top_symbols = [s for s, _ in symbol_counts.most_common(top_n)]
        top_categories = [
            {"key": c, "label": CATEGORY_LABELS.get(c, c), "count": n}
            for c, n in category_counts.most_common()
        ]
        top_exchanges = [{"name": e, "count": n} for e, n in exchange_counts.most_common()]

        return {
            "symbols": top_symbols,
            "categories": top_categories,
            "exchanges": top_exchanges,
        }

    # -- narrative -----------------------------------------------------------
    def _generate_narrative(self, events, groups) -> dict[str, Any]:
        if not events:
            return {
                "top_three": ["本周暂无竞品事件。"],
                "overview": "未抓取到竞品公告。",
                "impact_logic": "",
                "history_compare": "",
            }

        summary_lines = []
        for ev in events[:40]:
            summary_lines.append(
                f"[{ev.get('exchange')}|{LEVEL_LABELS.get(ev.get('impact_level','monitor'))}] "
                f"{ev.get('title')} ({ev.get('published_at')})"
            )
        data_block = "\n".join(summary_lines)

        if self.llm.available:
            reply = self.llm.chat(
                system_prompt=SCHEMA_INSTRUCTION,
                user_prompt=f"过去7天竞品事件（共{len(events)}条）：\n{data_block}",
                temperature=Config.LLM_TEMPERATURE_REPORT,
                json_mode=True,
            )
            parsed = LLMClient._safe_json(reply) if reply else None
            if parsed and parsed.get("top_three"):
                parsed.setdefault("overview", "")
                parsed.setdefault("impact_logic", "")
                parsed.setdefault("history_compare", "")
                return parsed
            logger.warning("LLM competitor narrative unavailable; using fallback.")

        return self._fallback_narrative(events, groups)

    @staticmethod
    def _fallback_narrative(events, groups) -> dict[str, Any]:
        immediate = groups.get("immediate", [])
        ordered = immediate + groups.get("confirm", []) + groups.get("management_align", [])
        top_three: list[str] = []
        for ev in ordered[:3]:
            cat = CATEGORY_LABELS.get(ev.get("category", ""), "动态")
            syms = "、".join((ev.get("symbols") or [])[:3])
            label = f"{ev.get('exchange')} {cat}" + (f"（{syms}）" if syms else "")
            reason = (ev.get("impact_reason") or "").strip()
            top_three.append(f"{label}：{reason}" if reason else label)
        while len(top_three) < 3:
            top_three.append("本周暂无更高优先级事件。")

        by_exchange: dict[str, int] = defaultdict(int)
        for ev in events:
            by_exchange[ev.get("exchange", "Unknown")] += 1
        exchange_txt = "、".join(f"{k} {v} 条" for k, v in by_exchange.items())

        overview = f"过去 7 天共捕获 {len(events)} 条竞品动态，来源分布：{exchange_txt}。"
        impact_logic = (
            "影响评估按「是否直接影响我方用户」排序：上币与费率调整列为立即响应；"
            "下架等需确认承接机会列为进一步确认；涉及跨部门协调的归入管理层对齐；"
            "活动类进入持续观察。"
        )
        history_compare = "本期为系统首次归纳窗口，历史对比数据将在积累多个周期后自动生成。"
        return {
            "top_three": top_three,
            "overview": overview,
            "impact_logic": impact_logic,
            "history_compare": history_compare,
        }
