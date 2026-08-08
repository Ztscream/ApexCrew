from __future__ import annotations

from apexcrew.adapters.repository.unified_diff import apply_unified_diff


def test_zero_old_line_hunk_inserts_after_existing_line() -> None:
    assert (
        apply_unified_diff(
            b"one\ntwo\n",
            "@@ -1,0 +2 @@\n+between\n",
        )
        == b"one\nbetween\ntwo\n"
    )
