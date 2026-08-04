from __future__ import annotations

from apexcrew.demo import build_demo_trace


def test_demo_reproduces_guard_feedback_and_freshness_behaviors() -> None:
    trace = build_demo_trace()
    assert [event["behavior"] for event in trace] == ["guard", "feedback", "freshness"]
    assert trace[0]["decision"] == "DENY"
    assert trace[1]["first_action"] != trace[1]["next_action"]
    assert trace[2]["status"] == "STALE"
