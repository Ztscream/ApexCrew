from __future__ import annotations

import json
import re
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
    required_csp = ("Content-Security-Policy", "default-src 'self'", "connect-src 'none'")
    if any(value not in index for value in required_csp):
        raise SystemExit("STATIC_REPLAY_CSP_MISSING")
    required_markers = (
        'data-view="lifecycle"',
        'data-view="tasks"',
        'data-view="evidence"',
        'data-view="authority"',
        'data-view="audit"',
        'data-control="play"',
        'data-control="pause"',
        'data-control="step"',
        'data-control="scrub"',
        'data-control="worker-filter"',
        'data-control="task-filter"',
    )
    if any(value not in index for value in required_markers):
        raise SystemExit("STATIC_REPLAY_VIEW_OR_CONTROL_MISSING")
    if any(
        value in script
        for value in (
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "sendBeacon",
            "innerHTML",
            "insertAdjacentHTML",
            "eval(",
            'method: "POST"',
        )
    ):
        raise SystemExit("STATIC_REPLAY_MUTATION_OR_SCRIPT_SINK")
    parser = ReplayDataParser()
    parser.feed(index)
    if not parser.replay_data:
        raise SystemExit("STATIC_REPLAY_RECORD_MISSING")
    replay = json.loads(parser.replay_data)
    if set(replay) != {
        "availability",
        "budget",
        "frames",
        "goal",
        "plan_revision",
        "repository",
        "run_id",
        "state",
        "tasks",
        "workers",
    }:
        raise SystemExit("STATIC_REPLAY_FIELDS_INVALID")
    frames = replay["frames"]
    frame_fields = {
        "authority",
        "category",
        "checks",
        "detail",
        "evidence",
        "sequence",
        "snapshot",
        "state",
        "time",
        "title",
    }
    records_are_allowlisted = (
        set(replay["budget"]) == {"elapsed", "model_calls", "tool_actions"}
        and all(set(frame) == frame_fields for frame in frames)
        and all(set(task) == {"id", "state", "worker"} for task in replay["tasks"])
        and all(set(worker) == {"id", "state"} for worker in replay["workers"])
    )
    sequences = [frame.get("sequence") for frame in frames]
    if (
        not records_are_allowlisted
        or replay["availability"] != "SANITIZED REPLAY"
        or replay["state"] != "COMPLETED"
        or len(replay["workers"]) != 2
        or len(replay["tasks"]) != 3
        or sequences != list(range(1, 10))
        or len(set(sequences)) != len(sequences)
        or frames[-1].get("state") != "COMPLETED"
        or frames[-1].get("evidence") != "FRESH"
        or frames[-1].get("authority") != "GRANT CONSUMED"
    ):
        raise SystemExit("STATIC_REPLAY_RECORD_INVALID")
    if 'href="styles.css"' not in index or 'src="app.js"' not in index:
        raise SystemExit("STATIC_REPLAY_SOURCE_ASSET_NAMES_INVALID")
    if re.search(r"app\.[0-9a-f]{12}\.js", index):
        raise SystemExit("STATIC_REPLAY_SOURCE_ASSET_ALREADY_HASHED")
    print("static-replay: clean")


if __name__ == "__main__":
    main()
