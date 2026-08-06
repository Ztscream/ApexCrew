from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _copy_regular_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("EXECUTOR_SNAPSHOT_UNSAFE")
    destination.mkdir(parents=True, exist_ok=False)
    for current, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        for name in directory_names:
            source_path = current_path / name
            if source_path.is_symlink():
                raise RuntimeError("EXECUTOR_SNAPSHOT_UNSAFE")
            (destination / relative / name).mkdir()
        for name in file_names:
            source_path = current_path / name
            if source_path.is_symlink() or not source_path.is_file():
                raise RuntimeError("EXECUTOR_SNAPSHOT_UNSAFE")
            destination_path = destination / relative / name
            with source_path.open("rb") as source_file, destination_path.open("wb") as target:
                shutil.copyfileobj(source_file, target, length=64 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = tuple(arguments.command)
    if command[:1] == ("--",):
        command = command[1:]
    if not command or any(not token or "\x00" in token for token in command):
        return 125
    try:
        executable = shutil.which(command[0], path=os.environ.get("PATH"))
        if executable is None:
            return 127
        executable = str(Path(executable).resolve(strict=True))
        _copy_regular_tree(arguments.input, arguments.workspace)
        os.chdir(arguments.workspace)
        os.execve(executable, list(command), os.environ.copy())
    except (FileNotFoundError, NotADirectoryError):
        return 127
    except (OSError, RuntimeError):
        return 125
    return 125


if __name__ == "__main__":
    sys.exit(main())
