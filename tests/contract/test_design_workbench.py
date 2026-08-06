from __future__ import annotations

from pathlib import Path


def test_design_workbench_is_a_concrete_read_only_artifact() -> None:
    document = Path("docs/design-workbench.md").read_text(encoding="utf-8")
    assert "Status: READ-ONLY ARTIFACT" in document
    assert "Goal" in document
    assert "Candidate Graph" in document
    assert "Evidence Requirements" in document
    assert "UI State Catalogue" in document
    assert "does not issue" in document
    assert "Grant" in document


def test_design_workbench_preserves_application_authority_boundary() -> None:
    document = Path("docs/design-workbench.md").read_text(encoding="utf-8")
    assert "does not issue Runtime Permit" in document
    for forbidden in ("Grant", "typed CAS request", "model call", "Git command", "Docker command"):
        assert forbidden in document
