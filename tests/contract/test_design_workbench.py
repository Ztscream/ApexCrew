from __future__ import annotations

from pathlib import Path


def test_design_workbench_is_explicitly_a_non_executing_stub() -> None:
    document = Path("docs/design-workbench.md").read_text(encoding="utf-8")
    assert "STUB" in document
    assert "does not issue" in document
    assert "Grant" in document
