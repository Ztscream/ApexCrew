from __future__ import annotations

from pathlib import Path


def test_release_artifacts_are_declared() -> None:
    dockerfile = Path("Dockerfile").read_text()
    makefile = Path("Makefile").read_text()
    gitlab = Path(".gitlab-ci.yml").read_text()
    workflow = Path(".github/workflows/ci.yml").read_text()
    assert "FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert "--network=none" in dockerfile
    assert all(
        f"{target}:" in makefile for target in ("test", "lint", "demo", "secret-scan", "build")
    )
    assert "\n\tuv build\n" in makefile
    assert "$(UV_RUN) build" not in makefile
    assert "unit-test:" in gitlab
    assert "uv run pytest" in gitlab
    assert "build:" in workflow
    assert "uv build" in workflow


def test_live_smoke_is_not_in_default_targets() -> None:
    makefile = Path("Makefile").read_text()
    assert "live-smoke:" in makefile
    assert "tests/integration/test_live_provider_smoke.py" in makefile
    assert "live-smoke" not in makefile.split("test:", 1)[1].split("\n\n", 1)[0]
