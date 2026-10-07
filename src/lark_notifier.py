"""Lark (Feishu) incoming-webhook notifier.

Supports two independent bots — one for the data report, one for competitor
intelligence — each identified by its own webhook URL.  The card body uses
Lark's interactive message-card schema (``msg_type: interactive``).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from src.config import Config

logger = logging.getLogger(__name__)


class LarkNotifier:
    """Thin wrapper for Lark/Feishu custom-bot webhooks."""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = Config.LARK_ENABLED if enabled is None else enabled

    # -- raw senders ---------------------------------------------------------
    def _post(self, webhook_url: str, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            logger.info("Lark notifications disabled; skipping send.")
            return False
        if not webhook_url:
            logger.warning("Lark webhook URL missing; skipping send.")
            return False
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            if body.get("code", 0) not in (0, None):
                logger.error("Lark API returned an error: %s", body)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send Lark message: %s", exc)
            return False

    def send_text(self, webhook_url: str, text: str) -> bool:
        return self._post(webhook_url, {"msg_type": "text", "content": {"text": text}})

    def send_card(
        self,
        webhook_url: str,
        title: str,
        content_md: str,
        buttons: list[dict[str, str]] | None = None,
        template: str = "blue",
    ) -> bool:
        """Send an interactive card.

        ``buttons`` is a list of ``{"text": ..., "url": ...}`` dicts rendered as
        a row of link buttons (typically "查看完整报告").
        """
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": content_md},
        ]
        if buttons:
            actions = [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": b.get("text", "查看详情")},
                    "url": b.get("url", ""),
                    "type": b.get("type", "primary"),
                }
                for b in buttons
                if b.get("url")
            ]
            if actions:
                elements.append({"tag": "action", "actions": actions})

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": template,
                },
                "elements": elements,
            },
        }
        return self._post(webhook_url, payload)

    # -- convenience ---------------------------------------------------------
    def send_weekly_card(self, title: str, content_md: str, buttons=None, template="blue") -> bool:
        return self.send_card(Config.LARK_WEBHOOK_WEEKLY, title, content_md, buttons, template)

    def send_competitor_card(self, title: str, content_md: str, buttons=None, template="turquoise") -> bool:
        return self.send_card(Config.LARK_WEBHOOK_COMPETITOR, title, content_md, buttons, template)

    def send_weekly_text(self, text: str) -> bool:
        return self.send_text(Config.LARK_WEBHOOK_WEEKLY, text)

    def send_competitor_text(self, text: str) -> bool:
        return self.send_text(Config.LARK_WEBHOOK_COMPETITOR, text)
