#!/usr/bin/env python3
"""Entry point: first-run bootstrap.

Generates ALL THREE reports (daily + weekly + monthly) and the competitor
report once, so the very first successful run presents every output type.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["all", *sys.argv[1:]]))
