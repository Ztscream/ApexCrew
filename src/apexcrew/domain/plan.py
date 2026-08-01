from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from apexcrew.domain.types import TaskId


class PathValidationError(ValueError):
    pass


_DOS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "clock$",
    "conin$",
    "conout$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
_PROTECTED_SEGMENTS = {".git", ".apexcrew"}


def _valid_segment(segment: str) -> bool:
    folded = unicodedata.normalize("NFC", segment).casefold()
    basename = folded.split(".", 1)[0]
    return (
        bool(segment)
        and segment not in {".", ".."}
        and not segment.endswith((".", " "))
        and not any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in segment)
        and not any(character in '<>"|?*\\:' for character in segment)
        and folded not in _PROTECTED_SEGMENTS
        and basename not in _DOS_NAMES
    )


class CanonicalPath(str):
    @classmethod
    def parse(cls, value: str) -> CanonicalPath:
        normalized = unicodedata.normalize("NFC", value)
        drive_qualified = re.match(r"^[A-Za-z]:", normalized) is not None
        segments = normalized.split("/")
        if (
            normalized != value
            or not normalized
            or normalized.startswith("/")
            or drive_qualified
            or not all(_valid_segment(segment) for segment in segments)
        ):
            raise PathValidationError("INVALID_CANONICAL_PATH")
        return cls(normalized)


class GlobValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GlobPattern:
    value: str
    segments: tuple[str, ...]
    matcher_version: str = "apexcrew-path-v1"

    @classmethod
    def parse(cls, value: str) -> GlobPattern:
        normalized = unicodedata.normalize("NFC", value)
        segments = tuple(normalized.split("/"))
        if (
            normalized != value
            or not normalized
            or normalized.startswith("/")
            or any(character in "?[]{}!\\" for character in normalized)
            or any("**" in segment and segment != "**" for segment in segments)
            or any(
                segment != "**" and not _valid_segment(segment.replace("*", "x"))
                for segment in segments
            )
        ):
            raise GlobValidationError("INVALID_GLOB_PATTERN")
        return cls(normalized, segments)

    def matches(self, path: CanonicalPath) -> bool:
        path_segments = path.split("/")
        return _match_segments(self.segments, tuple(path_segments))


def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return (
            _match_segments(pattern[1:], path) or bool(path) and _match_segments(pattern, path[1:])
        )
    if not path:
        return False
    expression = "".join(
        ".*" if character == "*" else re.escape(character) for character in pattern[0]
    )
    return re.fullmatch(expression, path[0]) is not None and _match_segments(pattern[1:], path[1:])


class GlobProof(StrEnum):
    PROVEN = "PROVEN"
    UNKNOWN = "UNKNOWN"


def _literal(pattern: GlobPattern) -> CanonicalPath | None:
    return None if "*" in pattern.value else CanonicalPath.parse(pattern.value)


def prove_included(left: GlobPattern, right: GlobPattern) -> GlobProof:
    literal = _literal(left)
    if left == right or right.segments == ("**",):
        return GlobProof.PROVEN
    if literal is not None and right.matches(literal):
        return GlobProof.PROVEN
    if (
        right.segments[-1:] == ("**",)
        and left.segments[: len(right.segments) - 1] == right.segments[:-1]
    ):
        return GlobProof.PROVEN
    return GlobProof.UNKNOWN


def prove_disjoint(left: GlobPattern, right: GlobPattern) -> GlobProof:
    left_literal = _literal(left)
    right_literal = _literal(right)
    if left_literal is not None and right_literal is not None:
        return GlobProof.PROVEN if left_literal != right_literal else GlobProof.UNKNOWN
    if left_literal is not None and not right.matches(left_literal):
        return GlobProof.PROVEN
    if right_literal is not None and not left.matches(right_literal):
        return GlobProof.PROVEN
    for left_segment, right_segment in zip(left.segments, right.segments, strict=False):
        if "*" not in left_segment + right_segment and left_segment != right_segment:
            return GlobProof.PROVEN
    return GlobProof.UNKNOWN


def may_overlap(left: GlobPattern, right: GlobPattern) -> bool:
    return prove_disjoint(left, right) is not GlobProof.PROVEN


@dataclass(frozen=True, slots=True)
class CheckDefinition:
    argv: tuple[str, ...]
    input_globs: tuple[GlobPattern, ...]


@dataclass(frozen=True, slots=True)
class TaskContract:
    task_id: TaskId
    dependency_task_ids: tuple[TaskId, ...]
    read_globs: tuple[GlobPattern, ...]
    dependency_globs: tuple[GlobPattern, ...]
    write_globs: tuple[GlobPattern, ...]
    checks: tuple[CheckDefinition, ...]
    constraints: tuple[str, ...]

    @classmethod
    def from_strings(
        cls,
        task_id: str,
        read_globs: Sequence[str],
        write_globs: Sequence[str],
        *,
        dependency_task_ids: Sequence[str] = (),
        dependency_globs: Sequence[str] = (),
        checks: Sequence[CheckDefinition] = (),
        constraints: Sequence[str] = (),
    ) -> TaskContract:
        return cls(
            TaskId(task_id),
            tuple(TaskId(value) for value in dependency_task_ids),
            tuple(GlobPattern.parse(value) for value in read_globs),
            tuple(GlobPattern.parse(value) for value in dependency_globs),
            tuple(GlobPattern.parse(value) for value in write_globs),
            tuple(checks),
            tuple(constraints),
        )


@dataclass(frozen=True, slots=True)
class PlanRevision:
    tasks: tuple[TaskContract, ...]
    proposed_promotion_order: tuple[TaskId, ...]


@dataclass(frozen=True, slots=True)
class PlanValidation:
    promotion_hazards: set[tuple[TaskId, TaskId]]


class PlanValidationError(ValueError):
    pass


def validate_plan(plan: PlanRevision) -> PlanValidation:
    task_ids = tuple(task.task_id for task in plan.tasks)
    if len(plan.tasks) > 12 or len(set(task_ids)) != len(plan.tasks):
        raise PlanValidationError("INVALID_TASK_SET")
    if len(plan.proposed_promotion_order) != len(task_ids) or set(
        plan.proposed_promotion_order
    ) != set(task_ids):
        raise PlanValidationError("PROMOTION_ORDER_REQUIRED")
    for task in plan.tasks:
        if any(
            not any(prove_included(write, read) is GlobProof.PROVEN for read in task.read_globs)
            for write in task.write_globs
        ):
            raise PlanValidationError("WRITE_NOT_COVERED_BY_READ")
        if any(not check.input_globs for check in task.checks):
            raise PlanValidationError("MISSING_CHECK_INPUT")
    hazards: set[tuple[TaskId, TaskId]] = set()
    for writer in plan.tasks:
        for reader in plan.tasks:
            check_inputs = tuple(
                pattern for check in reader.checks for pattern in check.input_globs
            )
            sensitivity = reader.read_globs + reader.dependency_globs + check_inputs
            if writer.task_id != reader.task_id and any(
                may_overlap(write, observed)
                for write in writer.write_globs
                for observed in sensitivity
            ):
                hazards.add((writer.task_id, reader.task_id))
    order = {task_id: index for index, task_id in enumerate(plan.proposed_promotion_order)}
    if any(order.get(left, -1) >= order.get(right, -1) for left, right in hazards):
        raise PlanValidationError("PROMOTION_ORDER_REQUIRED")
    _reject_cycle(plan.tasks, hazards)
    return PlanValidation(hazards)


def _reject_cycle(tasks: Sequence[TaskContract], hazards: set[tuple[TaskId, TaskId]]) -> None:
    known = {task.task_id for task in tasks}
    if any(set(task.dependency_task_ids) - known for task in tasks):
        raise PlanValidationError("UNKNOWN_DEPENDENCY")
    edges = set(hazards) | {
        (dependency, task.task_id) for task in tasks for dependency in task.dependency_task_ids
    }
    pending = set(known)
    while pending:
        ready = {
            node
            for node in pending
            if not any(right == node and left in pending for left, right in edges)
        }
        if not ready:
            raise PlanValidationError("PLAN_GRAPH_CYCLE")
        pending -= ready
