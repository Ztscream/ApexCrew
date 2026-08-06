from __future__ import annotations

from pathlib import Path


def main() -> None:
    root = Path(__file__).parents[1] / "webui"
    index = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    required = ("Content-Security-Policy", "default-src 'self'")
    if any(value not in index for value in required):
        raise SystemExit("STATIC_REPLAY_CSP_MISSING")
    if any(value in script for value in ("innerHTML", "eval(", 'method: "POST"')):
        raise SystemExit("STATIC_REPLAY_MUTATION_OR_SCRIPT_SINK")
    if 'fetch("/api/run"' not in script or 'method: "GET"' not in script:
        raise SystemExit("STATIC_REPLAY_READ_ROUTE_MISSING")
    print("static-replay: clean")


if __name__ == "__main__":
    main()
