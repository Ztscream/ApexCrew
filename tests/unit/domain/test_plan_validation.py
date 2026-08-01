import pytest

from apexcrew.domain.plan import (
    CheckDefinition,
    GlobPattern,
    PlanRevision,
    PlanValidationError,
    TaskContract,
    validate_plan,
)


def task(
    task_id: str,
    read_globs: tuple[str, ...] = (),
    write_globs: tuple[str, ...] = (),
    checks: tuple[CheckDefinition, ...] = (),
) -> TaskContract:
    return TaskContract.from_strings(
        task_id,
        read_globs or write_globs,
        write_globs,
        checks=checks,
    )


def check(*input_globs: str) -> CheckDefinition:
    return CheckDefinition(
        argv=("pytest", "-q"),
        input_globs=tuple(GlobPattern.parse(value) for value in input_globs),
    )


def make_plan(*tasks: TaskContract) -> PlanRevision:
    return PlanRevision(
        tasks=tasks,
        proposed_promotion_order=tuple(task.task_id for task in tasks),
    )


def test_writer_of_a_reader_input_creates_promotion_hazard() -> None:
    plan = make_plan(
        task("A", write_globs=("src/pricing.py",)),
        task("B", read_globs=("src/pricing.py",)),
    )
    assert validate_plan(plan).promotion_hazards == {("A", "B")}


def test_writer_of_a_check_input_creates_promotion_hazard() -> None:
    writer = task("A", write_globs=("generated/report.json",))
    checker = task(
        "B",
        read_globs=("docs/**",),
        checks=(check("generated/report.json"),),
    )
    assert "check_input_globs" not in TaskContract.__dataclass_fields__
    assert validate_plan(make_plan(writer, checker)).promotion_hazards == {("A", "B")}


def test_duplicate_promotion_entries_cannot_omit_a_hazard_writer() -> None:
    writer = task("A", write_globs=("generated/report.json",))
    checker = task(
        "B",
        read_globs=("docs/**",),
        checks=(check("generated/report.json"),),
    )
    plan = PlanRevision(
        tasks=(writer, checker),
        proposed_promotion_order=(checker.task_id, checker.task_id),
    )
    with pytest.raises(PlanValidationError, match="PROMOTION_ORDER_REQUIRED"):
        validate_plan(plan)
