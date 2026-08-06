from __future__ import annotations

import subprocess
from pathlib import Path


def test_static_webui_build_contains_only_read_route(tmp_path: Path) -> None:
    result = subprocess.run(
        ["uv", "run", "--python", "3.12", "python", "scripts/build_webui.py", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    script = (tmp_path / "app.js").read_text(encoding="utf-8")
    assert "ApexCrew" in index
    assert "Content-Security-Policy" in index
    assert "default-src 'self'" in index
    assert "/api/run" in script
    assert 'method: "GET"' in script
    assert 'credentials: "omit"' in script
    assert 'cache: "no-store"' in script
    assert "innerHTML" not in script
    assert "POST" not in script
