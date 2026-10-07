"""Gmail sender via the Gmail API using OAuth 2.0 desktop credentials.

Credential resolution order:

1. ``credentials/token.json`` if present and valid;
2. the same file refreshed when it is expired but has a refresh token;
3. an **interactive browser login** (opens the Google consent page) when
   ``GMAIL_INTERACTIVE_LOGIN`` is enabled and no token file exists yet;
4. a ``GMAIL_REFRESH_TOKEN`` read from the environment (headless fallback).

All failures are non-fatal: the pipeline reports the email status instead of
crashing, and outbound traffic honours ``NET_PROXY`` (see ``src.config``).
"""

from __future__ import annotations

import base64
import logging
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

    # -- credentials ---------------------------------------------------------
    def _write_client_secret_file(self, secret_path: Path) -> bool:
        """Materialise credentials/client_secret.json from env vars if needed."""
        if secret_path.exists():
            return True
        if not (Config.GMAIL_CLIENT_ID and Config.GMAIL_CLIENT_SECRET):
            return False
        import json

        payload = {
            "installed": {
                "client_id": Config.GMAIL_CLIENT_ID,
                "client_secret": Config.GMAIL_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": Config.GMAIL_TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        }
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("Wrote client secret file from env -> %s", secret_path)
        return True

    def _load_token(self, token_path: Path):
        from google.oauth2.credentials import Credentials

        if not token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load token file: %s", exc)
            return None

    def _from_refresh_token(self):
        from google.oauth2.credentials import Credentials

        if not Config.GMAIL_REFRESH_TOKEN:
            return None
        return Credentials(
            token=None,
            refresh_token=Config.GMAIL_REFRESH_TOKEN,
            token_uri=Config.GMAIL_TOKEN_URI,
            client_id=Config.GMAIL_CLIENT_ID or None,
            client_secret=Config.GMAIL_CLIENT_SECRET or None,
            scopes=SCOPES,
        )

    def _interactive_login(self, secret_path: Path, token_path: Path):
        """Open the Google consent page in the browser and capture the token."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self._write_client_secret_file(secret_path):
            logger.warning("No client secret available for interactive login.")
            return None

        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
        logger.info("Opening the Google consent page in your browser ...")
        try:
            creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=180)
        except TypeError:
            # Older google-auth-oauthlib without timeout_seconds.
            creds = flow.run_local_server(port=0, open_browser=True)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Saved OAuth token -> %s", token_path)
        return creds

    def login(self) -> tuple[bool, str]:
        """Force an interactive browser login. Returns ``(success, detail)``."""
        token_path = _resolve(Config.GMAIL_TOKEN_FILE)
        secret_path = _resolve(Config.GMAIL_CLIENT_SECRET_FILE)
        try:
            creds = self._interactive_login(secret_path, token_path)
            return (creds is not None), ("browser login complete" if creds else "login failed")
        except Exception as exc:  # noqa: BLE001
            logger.error("Interactive login failed: %s", exc)
            return False, str(exc)

    def _get_service(self):
        if self._service is not None:
            return self._service

        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        token_path = _resolve(Config.GMAIL_TOKEN_FILE)
        secret_path = _resolve(Config.GMAIL_CLIENT_SECRET_FILE)

        creds = self._load_token(token_path)

        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Token refresh failed: %s", exc)
                creds = None

        if not creds:
            creds = self._from_refresh_token()
            if creds:
                try:
                    creds.refresh(Request())
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    token_path.write_text(creds.to_json(), encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Refresh-token flow failed: %s", exc)
                    creds = None

        # Interactive browser login only when nothing else is available, so that
        # scheduled runs (with a stored refresh token) never block on a browser.
        if not creds and Config.GMAIL_INTERACTIVE_LOGIN:
            try:
                creds = self._interactive_login(secret_path, token_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Interactive login unavailable: %s", exc)
                creds = None

        if not creds:
            raise FileNotFoundError(
                "No usable Gmail credentials (token.json / refresh token / interactive login)."
            )

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
