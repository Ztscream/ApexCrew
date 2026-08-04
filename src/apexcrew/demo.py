from __future__ import annotations

import json
from typing import TypedDict

from apexcrew.domain.actions import ActionEnvelope
from apexcrew.domain.evidence import ContextCapsule, EvidenceReceipt
from apexcrew.domain.freshness import FreshnessAssessment
from apexcrew.domain.policy import ActionPolicy


class DemoEvent(TypedDict):
    behavior: str
    action: str
    decision: str
    first_action: str
    next_action: str
    status: str


def build_demo_trace() -> tuple[DemoEvent, ...]:
    denied = ActionPolicy.default().classify(ActionEnvelope(kind="raw_shell"))
    capsule = ContextCapsule.create(
        run_id="demo-run",
        task_id="demo-task",
        revision_digest="sha256:" + "1" * 64,
        dependencies=("sha256:" + "2" * 64,),
        content="fixture context",
    )
    receipt = EvidenceReceipt.create(capsule, result_class="CHECK_FAILED", result="red")
    freshness = FreshnessAssessment.assess(
        receipt,
        current_revision=receipt.revision_digest,
        current_dependencies=("sha256:" + "3" * 64,),
    )
    return (
        {
            "behavior": "guard",
            "action": "raw_shell",
            "decision": denied,
            "first_action": "",
            "next_action": "",
            "status": "",
        },
        {
            "behavior": "feedback",
            "action": "",
            "decision": "",
            "first_action": "check",
            "next_action": "patch",
            "status": "CHECK_FAILED_FEEDBACK",
        },
        {
            "behavior": "freshness",
            "action": "",
            "decision": "",
            "first_action": "",
            "next_action": "",
            "status": freshness.status,
        },
    )


def main() -> None:
    for event in build_demo_trace():
        print(json.dumps(event, sort_keys=True))


if __name__ == "__main__":
    main()
