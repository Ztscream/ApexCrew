"""Strict unified-diff application shared by workspace writers."""

from __future__ import annotations

import re

from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$")


def apply_unified_diff(original: bytes, unified_diff: str) -> bytes:
    try:
        source_lines = original.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise RepositoryUnsafeError("PROTECTED_PATCH_UTF8_REQUIRED") from error
    patch_lines = unified_diff.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    patch_index = 0
    saw_hunk = False
    while patch_index < len(patch_lines):
        line = patch_lines[patch_index]
        if line.startswith(("--- ", "+++ ")):
            patch_index += 1
            continue
        match = _HUNK_HEADER.match(line)
        if match is None:
            raise RepositoryUnsafeError("PROTECTED_PATCH_FORMAT_INVALID")
        saw_hunk = True
        try:
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
        except ValueError as error:
            raise RepositoryUnsafeError("PROTECTED_PATCH_FORMAT_INVALID") from error
        if old_count == 0:
            if old_start != 0:
                raise RepositoryUnsafeError("PROTECTED_PATCH_CONTEXT_MISMATCH")
            target_index = 0
        else:
            target_index = old_start - 1
        if target_index < source_index or target_index > len(source_lines):
            raise RepositoryUnsafeError("PROTECTED_PATCH_CONTEXT_MISMATCH")
        output.extend(source_lines[source_index:target_index])
        source_index = target_index
        consumed_old = 0
        produced_new = 0
        patch_index += 1
        while patch_index < len(patch_lines) and not patch_lines[patch_index].startswith("@@ "):
            patch_line = patch_lines[patch_index]
            if patch_line.startswith("\\ No newline at end of file"):
                patch_index += 1
                continue
            if not patch_line or patch_line[0] not in {" ", "+", "-"}:
                raise RepositoryUnsafeError("PROTECTED_PATCH_FORMAT_INVALID")
            marker, value = patch_line[0], patch_line[1:]
            if marker in {" ", "-"}:
                if source_index >= len(source_lines) or source_lines[source_index] != value:
                    raise RepositoryUnsafeError("PROTECTED_PATCH_CONTEXT_MISMATCH")
                source_index += 1
                consumed_old += 1
            if marker in {" ", "+"}:
                output.append(value)
                produced_new += 1
            patch_index += 1
        if consumed_old != old_count or produced_new != new_count:
            raise RepositoryUnsafeError("PROTECTED_PATCH_HUNK_COUNT_MISMATCH")
    if not saw_hunk:
        raise RepositoryUnsafeError("PROTECTED_PATCH_HUNK_REQUIRED")
    output.extend(source_lines[source_index:])
    return "".join(output).encode("utf-8")


def reverse_unified_diff(current: bytes, unified_diff: str) -> bytes:
    reversed_lines: list[str] = []
    for line in unified_diff.splitlines(keepends=True):
        match = _HUNK_HEADER.match(line)
        if match is not None:
            try:
                old_start = int(match.group(1))
                old_count = int(match.group(2) or "1")
                new_start = int(match.group(3))
                new_count = int(match.group(4) or "1")
            except ValueError as error:
                raise RepositoryUnsafeError("PROTECTED_PATCH_FORMAT_INVALID") from error
            newline = "\n" if line.endswith("\n") else ""
            reversed_lines.append(
                f"@@ -{new_start},{new_count} +{old_start},{old_count} @@{newline}"
            )
        elif line.startswith("+") and not line.startswith("+++"):
            reversed_lines.append("-" + line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            reversed_lines.append("+" + line[1:])
        else:
            reversed_lines.append(line)
    return apply_unified_diff(current, "".join(reversed_lines))
