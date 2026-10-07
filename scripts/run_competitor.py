#!/usr/bin/env python3
"""Entry point: run the competitor monitoring pipeline (daily)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["competitor", *sys.argv[1:]]))
