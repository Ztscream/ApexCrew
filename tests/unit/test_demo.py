from __future__ import annotations

from apexcrew.demo import build_demo_trace


def test_demo_reproduces_guard_feedback_and_freshness_behaviors() -> None:
    trace = build_demo_trace()
    assert [event["behavior"] for event in trace] == ["guard", "feedback", "freshness"]
    assert trace[0]["decision"] == "DENY"
    assert trace[1]["first_action"] != trace[1]["next_action"]
    assert trace[1]["model_calls"] == "2"
    assert '"code":"CHECK_FAILED"' in trace[1]["feedback_bound"]
    assert trace[2]["status"] == "STALE"


def test_demo_feedback_behavior_is_driven_by_the_real_worker_loop() -> None:
    feedback = next(event for event in build_demo_trace() if event["behavior"] == "feedback")

    assert feedback["loop_turns"] == "2"
    assert feedback["first_action"] == "check"
    assert feedback["next_action"] == "patch"
    assert feedback["status"] == "CHECK_FAILED"


def test_demo_failed_check_is_produced_by_a_real_tool_execution() -> None:
    feedback = next(event for event in build_demo_trace() if event["behavior"] == "feedback")

    assert feedback["tool_executions"] == "2"
    assert feedback["first_turn_result"] == "CHECK_FAILED"
    assert feedback["next_turn_result"] == "PATCH_APPLIED"


def test_demo_binds_the_observed_failure_into_the_next_model_request() -> None:
    feedback = next(event for event in build_demo_trace() if event["behavior"] == "feedback")

    assert feedback["feedback_role"] == "tool"
    assert '"code":"CHECK_FAILED"' in feedback["feedback_bound"]
    assert "expected 300 cents" in feedback["feedback_bound"]
    assert feedback["first_turn_feedback_absent"] == "true"


def test_demo_repair_reaches_the_workspace() -> None:
    feedback = next(event for event in build_demo_trace() if event["behavior"] == "feedback")

    assert feedback["repaired_path"] == "src/money.py"
    assert feedback["repaired"] == "true"
