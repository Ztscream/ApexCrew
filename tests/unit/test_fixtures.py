from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_python_money_fixture_declares_integer_cents() -> None:
    source = (ROOT / "fixtures" / "python-money" / "src" / "money.py").read_text()
    assert "int" in source
    assert "cents" in source
    assert "float" not in source


def test_typescript_timestamp_fixture_declares_milliseconds() -> None:
    source = (ROOT / "fixtures" / "typescript-time" / "src" / "time.ts").read_text()
    assert "milliseconds" in source
    assert "number" in source
