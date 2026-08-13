from __future__ import annotations

import json
import re
import subprocess
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


def build_webui(tmp_path: Path) -> tuple[str, str]:
    result = subprocess.run(
        ["uv", "run", "--python", "3.12", "python", "scripts/build_webui.py", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    script_match = re.search(r'<script src="(app\.[0-9a-f]{12}\.js)"></script>', index)
    style_match = re.search(r'<link rel="stylesheet" href="(styles\.[0-9a-f]{12}\.css)">', index)
    assert script_match is not None
    assert style_match is not None
    script = (tmp_path / script_match.group(1)).read_text(encoding="utf-8")
    assert {path.name for path in tmp_path.iterdir()} == {
        "index.html",
        script_match.group(1),
        style_match.group(1),
    }
    return index, script


def test_static_webui_build_contains_sanitized_evidence_console(tmp_path: Path) -> None:
    index, script = build_webui(tmp_path)
    assert "ApexCrew" in index
    assert "Content-Security-Policy" in index
    assert "default-src 'self'" in index
    assert "connect-src 'none'" in index
    for marker in (
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
    ):
        assert marker in index
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "sendBeacon",
        ".innerHTML",
        "insertAdjacentHTML",
        "eval(",
        'method: "POST"',
    ):
        assert forbidden not in script


def test_embedded_replay_describes_a_complete_harness_run(tmp_path: Path) -> None:
    index, _ = build_webui(tmp_path)
    parser = ReplayDataParser()
    parser.feed(index)
    replay = json.loads(parser.replay_data)
    assert set(replay) == {
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
    }
    assert replay["availability"] == "SANITIZED REPLAY"
    assert replay["state"] == "COMPLETED"
    assert len(replay["workers"]) == 2
    assert len(replay["tasks"]) == 3
    assert set(replay["budget"]) == {"elapsed", "model_calls", "tool_actions"}
    assert all(set(worker) == {"id", "state"} for worker in replay["workers"])
    assert all(set(task) == {"id", "state", "worker"} for task in replay["tasks"])
    assert all(
        set(frame)
        == {
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
        for frame in replay["frames"]
    )
    assert [frame["sequence"] for frame in replay["frames"]] == list(range(1, 10))
    assert replay["frames"][-1]["state"] == "COMPLETED"
    assert replay["frames"][-1]["evidence"] == "FRESH"
    assert replay["frames"][-1]["authority"] == "GRANT CONSUMED"
