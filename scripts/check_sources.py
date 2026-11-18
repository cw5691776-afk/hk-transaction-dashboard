#!/usr/bin/env python3
"""Exit non-zero unless every enabled provider returned records.

GitHub's shared runner IPs are occasionally challenged by 28Hse and Midland.
The daily workflow runs the updater repeatedly until this check passes, so a
transient block cannot publish a dataset with missing sources.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "data" / "update-report.json"
REQUIRED = ("centaline", "hse28", "midland")


def main() -> int:
    try:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"no readable update report: {exc}", file=sys.stderr)
        return 2
    counts = {key: value.get("records", 0) for key, value in report.get("sources", {}).items()}
    print("source counts:", counts)
    missing = [key for key in REQUIRED if not counts.get(key)]
    if missing:
        print(f"missing records from: {', '.join(missing)}", file=sys.stderr)
        return 1
    print("all sources returned records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
