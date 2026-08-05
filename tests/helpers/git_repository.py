import subprocess
from pathlib import Path


def make_git_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    return root


def commit_repository(root: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=ApexCrew acceptance",
            "-c",
            "user.email=acceptance@localhost",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
        check=True,
    )
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def make_git_repository_with_linked_worktree(tmp_path: Path) -> Path:
    root = make_git_repository(tmp_path)
    admin = root / ".git" / "worktrees" / "foreign"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(str(tmp_path / "foreign" / ".git"), encoding="utf-8")
    return root
