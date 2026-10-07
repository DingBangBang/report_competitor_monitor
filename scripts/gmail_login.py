#!/usr/bin/env python3
"""Force an interactive Gmail OAuth login (opens the Google consent page)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.email_notifier import EmailNotifier  # noqa: E402

if __name__ == "__main__":
    ok, detail = EmailNotifier().login()
    print(f"Gmail login: {'OK' if ok else 'FAILED'} - {detail}")
    raise SystemExit(0 if ok else 1)
