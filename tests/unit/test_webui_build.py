from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_static_webui_build_contains_sanitized_embedded_replay(tmp_path: Path) -> None:
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
    assert "ApexCrew" in index
    assert "Content-Security-Policy" in index
    assert "default-src 'self'" in index
    assert "connect-src 'none'" in index
    assert 'id="replay-data"' in index
    assert '"availability": "SANITIZED REPLAY"' in index
    assert '"run_id": "fixture-run-001"' in index
    assert '"sequence": 3' in index
    assert '"state": "COMPLETED"' in index
    assert "fetch(" not in script
    assert "POST" not in script
