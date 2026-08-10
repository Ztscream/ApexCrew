from __future__ import annotations

import sys
from pathlib import Path

from apexcrew.adapters.executor import runner


def test_runner_resolves_executable_before_switching_workspace(monkeypatch, tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    workspace = tmp_path / "workspace"
    executable = tmp_path / "bin" / "pytest"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    original_cwd = Path.cwd()
    observed: dict[str, object] = {}

    def resolve(command: str, *, path: str | None) -> str:
        observed["which_command"] = command
        observed["which_cwd"] = Path.cwd()
        observed["which_path"] = path
        return str(executable)

    def change_directory(path: Path) -> None:
        observed["chdir"] = path

    def exec_file(path: str, argv: list[str], environment: dict[str, str]) -> None:
        observed["exec"] = (path, argv, environment)

    monkeypatch.setattr(runner.shutil, "which", resolve)
    monkeypatch.setattr(runner, "_copy_regular_tree", lambda source, destination: None)
    monkeypatch.setattr(runner.os, "chdir", change_directory)
    monkeypatch.setattr(runner.os, "execve", exec_file)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apexcrew-runner",
            "--input",
            str(input_root),
            "--workspace",
            str(workspace),
            "--",
            "pytest",
            "-q",
        ],
    )

    assert runner.main() == 125
    assert observed["which_command"] == "pytest"
    assert observed["which_cwd"] == original_cwd
    assert observed["chdir"] == workspace
    exec_observation = observed["exec"]
    assert isinstance(exec_observation, tuple)
    assert exec_observation[:2] == (str(executable.resolve()), ["pytest", "-q"])
    assert exec_observation[2] == dict(runner.os.environ)
