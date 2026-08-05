from __future__ import annotations

import re
from pathlib import Path


def test_readme_has_required_delivery_sections_and_debt_inventory() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    security = Path("SECURITY.md").read_text(encoding="utf-8")
    for heading in ("项目简介", "安装", "运行", "分发命令", "目录结构", "安全边界"):
        assert f"## {heading}" in readme
    assert "not an execution service" in readme
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("src").rglob("*.py"))
    markers = set(re.findall(r"DEBT-[A-Z0-9-]+", source))
    docs = readme + security
    assert markers
    assert markers <= set(re.findall(r"DEBT-[A-Z0-9-]+", docs))


def test_local_cli_workflow_and_live_gate_are_documented() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "run-create" in readme
    assert "begin-planning" in readme
    assert "Runtime Permit" in readme
    assert 'APEXCREW_LIVE_SMOKE="1"' in readme
    assert "read-only WebUI" in readme
