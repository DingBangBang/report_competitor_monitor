#!/usr/bin/env python3
"""Entry point: run the data report pipeline.

Usage:
    python scripts/run_weekly.py            # frequency-based (auto)
    python scripts/run_weekly.py --type all # daily + weekly + monthly
    python scripts/run_weekly.py --bootstrap
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["report", *sys.argv[1:]]))
