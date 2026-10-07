"""Gmail sender via the Gmail API using OAuth 2.0 desktop credentials.

The first run requires user interaction (browser consent) to create
``token.json``; afterwards the refresh token is reused non-interactively, which
makes it safe to schedule on a server.  All failures are non-fatal: the pipeline
reports the email status instead of crashing.
"""

from __future__ import annotations

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from src.config import Config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else Config.PROJECT_ROOT / p  # type: ignore[attr-defined]


class EmailNotifier:
    """Sends HTML emails through the authenticated user's Gmail account."""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = Config.GMAIL_ENABLED if enabled is None else enabled
        self._service = None

    # -- auth ----------------------------------------------------------------
    def _get_service(self):
        if self._service is not None:
            return self._service

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        token_path = _resolve(Config.GMAIL_TOKEN_FILE)
        secret_path = _resolve(Config.GMAIL_CLIENT_SECRET_FILE)

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif secret_path.exists():
                flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
                # Works interactively on a desktop; on a headless server run
                # once locally and copy token.json across.
                creds = flow.run_local_server(port=0)
            else:
                raise FileNotFoundError(
                    f"Gmail client secret not found at {secret_path}. "
                    "Download OAuth desktop credentials from Google Cloud Console."
                )
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    # -- send ----------------------------------------------------------------
    def send_html(
        self,
        subject: str,
        html_body: str,
        recipients: list[str] | None = None,
        sender: str | None = None,
    ) -> tuple[bool, str]:
        """Send an HTML email. Returns ``(success, detail)``."""
        if not self.enabled:
            return False, "Gmail disabled"

        recipients = recipients or Config.recipients()
        if not recipients:
            return False, "No recipients configured"

        try:
            service = self._get_service()
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = sender or Config.GMAIL_SENDER or "me"
            message["To"] = ", ".join(recipients)
            message.attach(MIMEText(html_body, "html", "utf-8"))

            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            logger.info("Email sent to %s", recipients)
            return True, f"sent to {', '.join(recipients)}"
        except FileNotFoundError as exc:
            logger.warning("Gmail not configured: %s", exc)
            return False, "not configured"
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send email: %s", exc)
            return False, f"error: {exc}"
