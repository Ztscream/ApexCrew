from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = perf_counter()
    files = [Path("webui/index.html"), Path("webui/app.js"), Path("webui/styles.css")]
    bytes_read = sum(path.stat().st_size for path in files)
    elapsed_ms = (perf_counter() - started) * 1000
    report = {
        "revision": "working-tree",
        "bytes_read": bytes_read,
        "static_read_ms": round(elapsed_ms, 3),
        "threshold_ms": 1000,
    }
    if elapsed_ms > report["threshold_ms"]:
        raise SystemExit("REFERENCE_PERFORMANCE_BUDGET_EXCEEDED")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
