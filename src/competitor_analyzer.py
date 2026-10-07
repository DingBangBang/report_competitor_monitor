"""Impact-priority classification for competitor announcements.

For each newly fetched event, DeepSeek returns a strict JSON object:

    {
      "impact_level": "immediate|confirm|management_align|monitor",
      "impact_reason": "...",
      "suggested_action": "...",
      "urgency_score": 1-10
    }

A low temperature (0.1) keeps classifications stable across runs.  A rule-based
fallback is used when the LLM is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

from src.analyzer_labels import IMPACT_LEVELS, LEVEL_LABELS
from src.config import Config
from src.database import Database
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是加密交易所竞品情报分析师。请根据公告信息判断其对我们业务的影响优先级。
只输出**一个 JSON 对象**，不要任何多余文字：
{
  "impact_level": "immediate | confirm | management_align | monitor",
  "impact_reason": "一句话说明为什么（中文）",
  "suggested_action": "建议的具体动作（中文）",
  "urgency_score": 1到10的整数
}
分类标准：
- immediate（立即响应）：竞品上币/费率调整等直接影响我们的用户，需要马上动作。
- confirm（进一步确认）：需要验证竞品数据或确认影响范围。
- management_align（管理层对齐）：需要跨部门协调或涉及战略调整。
- monitor（持续观察）：低优先级，记录下来即可。"""


class CompetitorAnalyzer:
    """Runs LLM impact assessment over unanalyzed competitor events."""

    def __init__(self, db: Database | None = None, llm: LLMClient | None = None) -> None:
        self.db = db or Database()
        self.llm = llm or LLMClient()

    def analyze_pending(self) -> list[dict[str, Any]]:
        events = self.db.get_unanalyzed_events()
        results = []
        for ev in events:
            analysis = self.analyze_event(ev)
            self.db.update_event_analysis(
                event_id=ev["event_id"],
                impact_level=analysis["impact_level"],
                impact_reason=analysis["impact_reason"],
                suggested_action=analysis["suggested_action"],
                urgency_score=analysis["urgency_score"],
            )
            ev.update(analysis)
            results.append(ev)
        logger.info("Analyzed %s competitor events.", len(results))
        return results

    def analyze_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if self.llm.available:
            user_prompt = (
                f"交易所: {event.get('exchange')}\n"
                f"类型: {event.get('category')}\n"
                f"标题: {event.get('title')}\n"
                f"涉及币种: {', '.join(event.get('symbols') or []) or '无'}\n"
                f"发布时间: {event.get('published_at')}"
            )
            parsed = self.llm.chat_json(SYSTEM_PROMPT, user_prompt, temperature=Config.LLM_TEMPERATURE_ANALYZER)
            if parsed and parsed.get("impact_level") in IMPACT_LEVELS:
                return {
                    "impact_level": parsed["impact_level"],
                    "impact_reason": parsed.get("impact_reason", ""),
                    "suggested_action": parsed.get("suggested_action", ""),
                    "urgency_score": int(parsed.get("urgency_score", 5)),
                }
            logger.warning("LLM analysis unavailable/invalid; using rule-based fallback.")

        return self._rule_based(event)

    # -- fallback ------------------------------------------------------------
    @staticmethod
    def _rule_based(event: dict[str, Any]) -> dict[str, Any]:
        category = event.get("category", "activity")
        if category in ("new_listing", "fee_change"):
            return {
                "impact_level": "immediate",
                "impact_reason": f"竞品{('上币' if category == 'new_listing' else '费率调整')}可能直接分流我们的用户与流动性。",
                "suggested_action": "核对相关币种流动性与我方费率竞争力，评估是否需要跟进上币或补贴。",
                "urgency_score": 8,
            }
        if category == "delisting":
            return {
                "impact_level": "confirm",
                "impact_reason": "竞品下架可能带来迁移用户与流动性，需要确认影响范围。",
                "suggested_action": "确认被下架币种并评估承接用户/资产的运营机会。",
                "urgency_score": 6,
            }
        return {
            "impact_level": "monitor",
            "impact_reason": "活动类公告短期影响有限，持续观察即可。",
            "suggested_action": "记录竞品活动力度，纳入月度对比。",
            "urgency_score": 3,
        }
