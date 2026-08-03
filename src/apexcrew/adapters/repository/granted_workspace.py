from __future__ import annotations

import os
import re
import stat
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Literal

from apexcrew.adapters.repository.no_follow import RepositoryUnsafeError
from apexcrew.domain.actions import RiskyAction
from apexcrew.domain.admission import RepositoryEffectUncertain
from apexcrew.domain.effects import canonical_json, sha256_digest
from apexcrew.domain.plan import CanonicalPath
from apexcrew.domain.policy import SecretPathPolicy
from apexcrew.domain.revisions import Sha256DigestText
from apexcrew.domain.tools import ActionPreState, GrantedActionObservation, ToolResult

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$")


class GrantedWorkspaceAdapter:
    def __init__(self, root: Path, secret_paths: SecretPathPolicy) -> None:
        self._root = root.resolve(strict=True)
        self._secret_paths = secret_paths

    @staticmethod
    def _is_link_or_reparse(metadata: os.stat_result) -> bool:
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)

    def _path(self, raw: str, *, require_protected: bool = False) -> Path:
        canonical = CanonicalPath.parse(raw)
        if self._secret_paths.inspect(canonical).code != "ALLOW":
            raise RepositoryUnsafeError("GRANTED_SECRET_PATH_DENIED")
        protected = str(canonical) == ".gitlab-ci.yml" or str(canonical).startswith(
            ".github/workflows/"
        )
        if require_protected != protected:
            raise RepositoryUnsafeError("GRANTED_PROTECTED_SCOPE_MISMATCH")
        current = self._root
        for component in str(canonical).split("/")[:-1]:
            current = current / component
            metadata = current.lstat()
            if self._is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise RepositoryUnsafeError("GRANTED_NO_FOLLOW_DENIED")
        return self._root.joinpath(*str(canonical).split("/"))

    def _regular(self, path: Path) -> tuple[bytes, int]:
        metadata = path.lstat()
        if self._is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise RepositoryUnsafeError("GRANTED_REGULAR_FILE_REQUIRED")
        content = path.read_bytes()
        if path.lstat() != metadata:
            raise RepositoryUnsafeError("GRANTED_FILE_CHANGED_DURING_READ")
        return content, stat.S_IMODE(metadata.st_mode)

    def _regular_if_present(self, path: Path) -> tuple[bytes, int] | None:
        try:
            return self._regular(path)
        except FileNotFoundError:
            return None

    @staticmethod
    def _digest(content: bytes) -> Sha256DigestText:
        return Sha256DigestText("sha256:" + sha256(content).hexdigest())

    @classmethod
    def _matches_source(cls, content: bytes, mode: int, expected: ActionPreState) -> bool:
        return (
            expected.source_digest is not None
            and cls._digest(content) == expected.source_digest
            and (expected.source_mode is None or mode == expected.source_mode)
        )

    @staticmethod
    def _apply_unified_diff(original: bytes, unified_diff: str) -> bytes:
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
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
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

    @staticmethod
    def _reverse_unified_diff(current: bytes, unified_diff: str) -> bytes:
        reversed_lines: list[str] = []
        for line in unified_diff.splitlines(keepends=True):
            match = _HUNK_HEADER.match(line)
            if match is not None:
                old_start = int(match.group(1))
                old_count = int(match.group(2) or "1")
                new_start = int(match.group(3))
                new_count = int(match.group(4) or "1")
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
        return GrantedWorkspaceAdapter._apply_unified_diff(current, "".join(reversed_lines))

    @staticmethod
    def _observation_digest(
        action: RiskyAction,
        state: str,
        source_digest: str | None,
        source_mode: int | None,
        destination_digest: str | None,
    ) -> Sha256DigestText:
        return sha256_digest(
            canonical_json(
                {
                    "destination_digest": destination_digest,
                    "operation": action.operation,
                    "path": action.path,
                    "source_digest": source_digest,
                    "source_mode": source_mode,
                    "state": state,
                }
            )
        )

    def observe(self, action: RiskyAction, expected: ActionPreState) -> GrantedActionObservation:
        try:
            source = self._path(
                action.path, require_protected=action.operation == "protected_patch"
            )
            source_entry = self._regular_if_present(source)
            source_exists = source_entry is not None
            source_content: bytes | None = None
            source_mode: int | None = None
            if source_entry is not None:
                source_content, source_mode = source_entry
            destination_content: bytes | None = None
            destination_exists = False
            if action.destination is not None:
                destination = self._path(action.destination)
                destination_entry = self._regular_if_present(destination)
                destination_exists = destination_entry is not None
                if destination_entry is not None:
                    destination_content, _ = destination_entry
            source_digest = None if source_content is None else str(self._digest(source_content))
            destination_digest = (
                None if destination_content is None else str(self._digest(destination_content))
            )
            exact_pre = (
                source_content is not None
                and source_mode is not None
                and self._matches_source(source_content, source_mode, expected)
                and (
                    action.destination is None
                    or expected.destination_absent
                    and not destination_exists
                )
            )
            post_result: ToolResult | None = None
            exact_post = False
            if action.operation == "delete":
                exact_post = not source_exists
                if exact_post:
                    post_result = ToolResult(code="DELETED")
            elif action.operation == "rename":
                exact_post = (
                    not source_exists
                    and expected.source_digest is not None
                    and destination_content is not None
                    and self._digest(destination_content) == expected.source_digest
                )
                if exact_post:
                    post_result = ToolResult(code="RENAMED", content_digest=expected.source_digest)
            elif action.operation == "set_executable" and source_content is not None:
                desired = bool(action.executable)
                exact_post = (
                    self._digest(source_content) == expected.source_digest
                    and bool(source_mode and source_mode & 0o111) == desired
                )
                if exact_post:
                    post_result = ToolResult(
                        code="EXECUTABLE_CHANGED",
                        content_digest=self._digest(source_content),
                    )
            elif action.operation == "protected_patch" and source_content is not None:
                assert action.unified_diff is not None
                if expected.source_digest is not None and not exact_pre:
                    try:
                        previous_content = self._reverse_unified_diff(
                            source_content, action.unified_diff
                        )
                    except RepositoryUnsafeError:
                        previous_content = None
                    exact_post = (
                        previous_content is not None
                        and self._digest(previous_content) == expected.source_digest
                    )
                if exact_post:
                    post_result = ToolResult(
                        code="PROTECTED_PATCH_APPLIED",
                        content_digest=self._digest(source_content),
                    )
            state: Literal["EXACT_PRE", "EXACT_POST", "THIRD"] = (
                "EXACT_PRE" if exact_pre else "EXACT_POST" if exact_post else "THIRD"
            )
            return GrantedActionObservation(
                state=state,
                digest=self._observation_digest(
                    action,
                    state,
                    source_digest,
                    source_mode,
                    destination_digest,
                ),
                post_result=post_result,
            )
        except (OSError, RepositoryUnsafeError, ValueError):
            return GrantedActionObservation(
                state="UNAVAILABLE",
                digest=self._observation_digest(action, "UNAVAILABLE", None, None, None),
            )

    def delete_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        try:
            path = self._path(action.path)
            content, mode = self._regular(path)
        except (OSError, RepositoryUnsafeError, ValueError):
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        if action.operation != "delete" or not self._matches_source(content, mode, expected):
            return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
        try:
            path.unlink()
        except OSError as error:
            raise RepositoryEffectUncertain("GRANTED_DELETE_UNCERTAIN") from error
        return ToolResult(code="DELETED", content_digest=self._digest(content))

    def rename_regular_file(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        if action.destination is None:
            return ToolResult.indeterminate("GRANTED_RENAME_DESTINATION_REQUIRED")
        try:
            source = self._path(action.path)
            destination = self._path(action.destination)
            content, mode = self._regular(source)
            destination_entry = self._regular_if_present(destination)
        except (OSError, RepositoryUnsafeError, ValueError):
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        if (
            action.operation != "rename"
            or not expected.destination_absent
            or destination_entry is not None
            or not self._matches_source(content, mode, expected)
        ):
            return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
        try:
            source.rename(destination)
        except OSError as error:
            raise RepositoryEffectUncertain("GRANTED_RENAME_UNCERTAIN") from error
        return ToolResult(code="RENAMED", content_digest=self._digest(content))

    def set_executable(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        try:
            path = self._path(action.path)
            content, mode = self._regular(path)
        except (OSError, RepositoryUnsafeError, ValueError):
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        if (
            action.operation != "set_executable"
            or action.executable is None
            or not self._matches_source(content, mode, expected)
        ):
            return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
        if os.name == "nt":
            return ToolResult.indeterminate("GRANTED_EXECUTABLE_MODE_UNSUPPORTED")
        target_mode = mode | 0o111 if action.executable else mode & ~0o111
        try:
            path.chmod(target_mode, follow_symlinks=False)
        except OSError as error:
            raise RepositoryEffectUncertain("GRANTED_EXECUTABLE_CHANGE_UNCERTAIN") from error
        return ToolResult(code="EXECUTABLE_CHANGED", content_digest=self._digest(content))

    def apply_protected_patch(self, action: RiskyAction, expected: ActionPreState) -> ToolResult:
        try:
            path = self._path(action.path, require_protected=True)
            content, mode = self._regular(path)
            if action.operation != "protected_patch" or action.unified_diff is None:
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            if not self._matches_source(content, mode, expected):
                return ToolResult.indeterminate("GRANTED_PRESTATE_MISMATCH")
            patched = self._apply_unified_diff(content, action.unified_diff)
        except (OSError, RepositoryUnsafeError, ValueError):
            return ToolResult.indeterminate("GRANTED_PREFLIGHT_DENIED")
        temporary_fd: int | None = None
        temporary_path: str | None = None
        applied = False
        try:
            temporary_fd, temporary_path = tempfile.mkstemp(
                prefix=".apexcrew-granted-", dir=path.parent
            )
            with os.fdopen(temporary_fd, "wb") as stream:
                temporary_fd = None
                stream.write(patched)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, path)
            applied = True
            temporary_path = None
        except OSError as error:
            if applied or not path.exists():
                raise RepositoryEffectUncertain("GRANTED_PROTECTED_PATCH_UNCERTAIN") from error
            return ToolResult.indeterminate("GRANTED_PROTECTED_PATCH_FAILED")
        finally:
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        return ToolResult(code="PROTECTED_PATCH_APPLIED", content_digest=self._digest(patched))
