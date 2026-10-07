"""Centralised configuration loader.

Reads all settings from ``environment.env`` at the project root (falling back to
normal environment variables when the file is absent).  Nothing here performs
network I/O, so importing :mod:`src.config` is always safe.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "environment.env"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
REPORTS_DIR = PROJECT_ROOT / "reports"
CREDENTIALS_DIR = PROJECT_ROOT / "credentials"
DATA_DIR = PROJECT_ROOT / "data"

# Load environment.env (does not override already-set env vars).
load_dotenv(dotenv_path=ENV_FILE, override=False)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class Config:
    """Runtime configuration snapshot read from the environment."""

    # --- Paths --------------------------------------------------------------
    PROJECT_ROOT: Path = PROJECT_ROOT
    TEMPLATES_DIR: Path = TEMPLATES_DIR
    CREDENTIALS_DIR: Path = CREDENTIALS_DIR

    # --- DeepSeek LLM -------------------------------------------------------
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    LLM_TEMPERATURE_REPORT: float = float(os.getenv("LLM_TEMPERATURE_REPORT", "0.6"))
    LLM_TEMPERATURE_ANALYZER: float = float(os.getenv("LLM_TEMPERATURE_ANALYZER", "0.1"))
    LLM_TIMEOUT: int = _int(os.getenv("LLM_TIMEOUT"), 60)
    LLM_MAX_RETRIES: int = _int(os.getenv("LLM_MAX_RETRIES"), 3)

    # --- Lark webhooks ------------------------------------------------------
    LARK_WEBHOOK_WEEKLY: str = os.getenv("LARK_WEBHOOK_WEEKLY", "")
    LARK_WEBHOOK_COMPETITOR: str = os.getenv("LARK_WEBHOOK_COMPETITOR", "")
    LARK_ENABLED: bool = _bool(os.getenv("LARK_ENABLED"), True)

    # --- Gmail --------------------------------------------------------------
    GMAIL_CLIENT_ID: str = os.getenv("GMAIL_CLIENT_ID", "")
    GMAIL_CLIENT_SECRET: str = os.getenv("GMAIL_CLIENT_SECRET", "")
    GMAIL_REFRESH_TOKEN: str = os.getenv("GMAIL_REFRESH_TOKEN", "")
    GMAIL_TOKEN_URI: str = os.getenv("GMAIL_OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token")
    GMAIL_CLIENT_SECRET_FILE: str = os.getenv(
        "GMAIL_CLIENT_SECRET_FILE", "credentials/client_secret.json"
    )
    GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "credentials/token.json")
    GMAIL_SENDER: str = os.getenv("GMAIL_SENDER", "")
    GMAIL_RECIPIENTS: str = os.getenv("GMAIL_RECIPIENTS", "dingbangchu@gmail.com")
    GMAIL_ENABLED: bool = _bool(os.getenv("GMAIL_ENABLED"), True)
    # Whether to open the Google consent page in the browser when authorising.
    GMAIL_INTERACTIVE_LOGIN: bool = _bool(os.getenv("GMAIL_INTERACTIVE_LOGIN"), True)

    # --- Network proxy ------------------------------------------------------
    NET_PROXY: str = os.getenv("NET_PROXY", "") or os.getenv("HTTPS_PROXY", "")


    # --- Reports / DB -------------------------------------------------------
    REPORTS_DIR: Path = Path(os.getenv("REPORTS_DIR", str(REPORTS_DIR)))
    DB_PATH: str = os.getenv("DB_PATH", str(DATA_DIR / "report_monitor.db"))
    REPORT_BASE_URL: str = os.getenv("REPORT_BASE_URL", "")

    # --- macOS notifications ------------------------------------------------
    ENABLE_MAC_NOTIFY: bool = _bool(os.getenv("ENABLE_MAC_NOTIFY"), True)

    @classmethod
    def recipients(cls) -> list[str]:
        return [r.strip() for r in cls.GMAIL_RECIPIENTS.split(",") if r.strip()]


# Ensure output directories exist on import (cheap and idempotent).
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

# Apply the outbound proxy (if configured) so that requests-based clients —
# including the Google OAuth token exchange and Gmail API — route through it.
if Config.NET_PROXY:
    for _var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.setdefault(_var, Config.NET_PROXY)
