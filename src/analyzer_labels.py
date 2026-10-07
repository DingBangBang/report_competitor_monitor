"""Shared labels/ordering for competitor impact levels."""

from __future__ import annotations

LEVEL_LABELS = {
    "immediate": "立即响应",
    "confirm": "进一步确认",
    "management_align": "管理层对齐",
    "monitor": "持续观察",
}

# Render order (highest priority first).
GROUP_ORDER = ["immediate", "confirm", "management_align", "monitor"]

IMPACT_LEVELS = list(GROUP_ORDER)

CATEGORY_LABELS = {
    "new_listing": "上币",
    "delisting": "下架",
    "fee_change": "费率调整",
    "activity": "活动",
}
