from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from apexcrew.application.runtime import FileRunOwnership
from apexcrew.domain.types import RunId, RuntimeOwnerId


class NeverLock:
    @contextmanager
    def try_lock(self, path: Path) -> Iterator[bool]:
        del path
        yield False


class FixedOwnerIds:
    def next_runtime_owner_id(self) -> RuntimeOwnerId:
        return RuntimeOwnerId("owner-1")


def test_invalid_runtime_permit_leaves_lock_root_unchanged(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    ownership = FileRunOwnership(data_root, NeverLock(), FixedOwnerIds())

    with ownership.acquire(RunId("run-1"), permit=None) as owner:
        assert owner is None

    assert not data_root.exists()
