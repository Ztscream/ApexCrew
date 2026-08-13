# tests/contract/test_bootstrap_ci.py
from pathlib import Path


def test_minimal_ci_runs_quality_and_offline_tests_on_every_push() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "quality:" in workflow
    assert "unit-ubuntu:" in workflow
    assert "unit-windows:" in workflow
    assert "uv run ruff format --check ." in workflow
    assert "uv run ruff check ." in workflow
    assert "uv run mypy src" in workflow
    assert workflow.count("uv run pytest") == 3
    for job in (
        "integration:",
        "pages:",
        "browser-quality:",
        "reference-performance:",
    ):
        assert job in workflow
    assert "needs: [quality, unit-ubuntu, unit-windows, integration, build, pages]" in workflow
    assert "actions/deploy-pages" not in workflow
    assert "OPENAI_API_KEY" not in workflow


def test_pages_workflow_deploys_only_a_successful_main_ci_revision() -> None:
    workflow = Path(".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert "workflow_run:" in workflow
    assert 'workflows: ["ci"]' in workflow
    assert "branches: [main]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "make web-build" in workflow
    assert "actions/upload-pages-artifact@v3" in workflow
    assert "actions/deploy-pages@v4" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
