"""macOS desktop notifications via ``terminal-notifier`` (with osascript fallback)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime

from src.config import Config

logger = logging.getLogger(__name__)


class MacNotifier:
    """Fires native macOS notifications summarising a pipeline run."""

    APP_TITLE = "Report & Competitor Monitor"

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = Config.ENABLE_MAC_NOTIFY if enabled is None else enabled
        self._has_terminal_notifier = shutil.which("terminal-notifier") is not None

    def notify(self, title: str, message: str, subtitle: str = "") -> bool:
        if not self.enabled:
            logger.info("macOS notifications disabled; skipping.")
            return False
        try:
            if self._has_terminal_notifier:
                cmd = ["terminal-notifier", "-title", title, "-message", message]
                if subtitle:
                    cmd += ["-subtitle", subtitle]
                cmd += ["-group", self.APP_TITLE]
                subprocess.run(cmd, check=False, capture_output=True)
            else:
                script = (
                    f'display notification {self._as_applescript(message)} '
                    f'with title {self._as_applescript(title)}'
                )
                if subtitle:
                    script += f' subtitle {self._as_applescript(subtitle)}'
                subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send macOS notification: %s", exc)
            return False

    def notify_run_summary(
        self,
        conclusion: str,
        status: str,
        email_status: str,
        lark_status: str,
        finished_at: datetime | None = None,
    ) -> bool:
        """Send the standard 5-field run summary notification."""
        ended = (finished_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        message = (
            f"{conclusion}\n"
            f"状态: {status} | 结束: {ended}\n"
            f"邮件: {email_status} | Lark: {lark_status}"
        )
        emoji = "✅" if status.upper().startswith("SUCCESS") or status == "成功" else "❌"
        return self.notify(f"{emoji} {self.APP_TITLE}", message, subtitle=finished_at and ended or "")

    @staticmethod
    def _as_applescript(text: str) -> str:
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
