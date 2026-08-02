import subprocess
from pathlib import Path


def make_git_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    return root


def make_git_repository_with_linked_worktree(tmp_path: Path) -> Path:
    root = make_git_repository(tmp_path)
    admin = root / ".git" / "worktrees" / "foreign"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(str(tmp_path / "foreign" / ".git"), encoding="utf-8")
    return root
