"""DeepSeek LLM client (OpenAI-compatible chat completions API).

The client degrades gracefully: when ``DEEPSEEK_API_KEY`` is missing or the API
call fails after retries, callers receive ``None`` and are expected to fall back
to the deterministic templates in the generator modules.  This keeps the whole
pipeline runnable in offline / CI / first-run environments.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from src.config import Config

logger = logging.getLogger(__name__)


class LLMClient:
    """Minimal chat-completion wrapper around the DeepSeek API."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or Config.DEEPSEEK_API_KEY
        self.base_url = Config.DEEPSEEK_BASE_URL.rstrip("/")
        self.model = Config.DEEPSEEK_MODEL
        self.timeout = Config.LLM_TIMEOUT
        self.max_retries = Config.LLM_MAX_RETRIES

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # -- public API ----------------------------------------------------------
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.6,
        json_mode: bool = False,
    ) -> str | None:
        """Return the assistant message content, or ``None`` on failure."""
        if not self.available:
            logger.warning("DEEPSEEK_API_KEY not set; LLM call skipped.")
            return None

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    url, headers=headers, data=json.dumps(payload), timeout=self.timeout
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except Exception as exc:  # noqa: BLE001 - want to retry on anything
                logger.warning("LLM call failed (attempt %s/%s): %s", attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
        logger.error("LLM call exhausted all retries; falling back.")
        return None

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
    ) -> dict[str, Any] | None:
        """Chat and parse the reply as JSON, tolerating code fences."""
        raw = self.chat(system_prompt, user_prompt, temperature=temperature, json_mode=True)
        if raw is None:
            return None
        return self._safe_json(raw)

    @staticmethod
    def _safe_json(text: str) -> dict[str, Any] | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Best effort: grab the outermost {...} block.
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    pass
        logger.error("Could not parse JSON from LLM reply: %s", text[:200])
        return None
