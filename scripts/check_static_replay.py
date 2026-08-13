from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path


class ReplayDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_replay_data = False
        self.replay_data = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_replay_data = tag == "script" and dict(attrs).get("id") == "replay-data"

    def handle_data(self, data: str) -> None:
        if self._in_replay_data:
            self.replay_data += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_replay_data = False


def main() -> None:
    root = Path(__file__).parents[1] / "webui"
    index = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    required = ("Content-Security-Policy", "default-src 'self'", "connect-src 'none'")
    if any(value not in index for value in required):
        raise SystemExit("STATIC_REPLAY_CSP_MISSING")
    if any(
        value in script
        for value in (
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "sendBeacon",
            "innerHTML",
            "eval(",
            'method: "POST"',
        )
    ):
        raise SystemExit("STATIC_REPLAY_MUTATION_OR_SCRIPT_SINK")
    parser = ReplayDataParser()
    parser.feed(index)
    if not parser.replay_data:
        raise SystemExit("STATIC_REPLAY_RECORD_MISSING")
    if json.loads(parser.replay_data) != {
        "availability": "SANITIZED REPLAY",
        "run_id": "fixture-run-001",
        "sequence": 3,
        "state": "COMPLETED",
    }:
        raise SystemExit("STATIC_REPLAY_FIELDS_INVALID")
    print("static-replay: clean")


if __name__ == "__main__":
    main()
