from __future__ import annotations

from pathlib import Path
from subprocess import run


def test_secret_scan_passes_current_tree() -> None:
    root = Path(__file__).parents[1]
    result = run(
        ["uv", "run", "--python", "3.12", "python", "scripts/secret_scan.py", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret-scan: clean" in result.stdout
