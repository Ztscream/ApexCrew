from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PATTERNS = (
    r"sk-[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9_]{20,}",
)
COMBINED_PATTERN = "(" + "|".join(PATTERNS) + ")"


def _git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def _contains_match(root: Path, revision: str | None) -> bool:
    target = revision or "HEAD"
    result = _git(root, "grep", "--no-color", "-n", "-I", "-E", COMBINED_PATTERN, target, "--")
    if result.returncode > 1:
        raise RuntimeError(result.stderr.strip() or "GIT_SCAN_FAILED")
    return result.returncode == 0


def scan(root: Path) -> int:
    if _git(root, "rev-parse", "--show-toplevel").returncode != 0:
        print("secret-scan: not a git repository", file=sys.stderr)
        return 2
    revisions = _git(root, "rev-list", "--all")
    if revisions.returncode != 0:
        print("secret-scan: unable to enumerate history", file=sys.stderr)
        return 2
    if _contains_match(root, None):
        print("secret-scan: findings in tracked tree", file=sys.stderr)
        return 1
    for revision in revisions.stdout.splitlines():
        if _contains_match(root, revision):
            print("secret-scan: finding in reachable history", file=sys.stderr)
            return 1
    print("secret-scan: clean")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    return scan(parser.parse_args().root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
