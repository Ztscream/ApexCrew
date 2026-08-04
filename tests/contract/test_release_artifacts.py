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
    assert "unit-test:" in gitlab
    assert "uv run pytest" in gitlab
    assert "build:" in workflow
    assert "uv build" in workflow
