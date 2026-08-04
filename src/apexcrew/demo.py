from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import TypedDict

from apexcrew.adapters.model.scripted import ScriptedMockLLM, ScriptedModelStep
from apexcrew.domain.actions import ActionEnvelope
from apexcrew.domain.evidence import ContextCapsule, EvidenceReceipt
from apexcrew.domain.freshness import FreshnessAssessment
from apexcrew.domain.model import ModelCompletion, ModelRequest, ModelUsage, ProviderAttemptResult
from apexcrew.domain.policy import ActionPolicy
from apexcrew.domain.types import RevisionDigest, RunId


class DemoEvent(TypedDict):
    behavior: str
    action: str
    decision: str
    first_action: str
    next_action: str
    status: str
    model_calls: str
    feedback_bound: str


def build_demo_trace() -> tuple[DemoEvent, ...]:
    denied = ActionPolicy.default().classify(ActionEnvelope(kind="raw_shell"))
    request = ModelRequest(
        run_id=RunId("demo-run"),
        plan_digest=None,
        policy_digest=RevisionDigest("sha256:" + "1" * 64),
        budget_digest=RevisionDigest("sha256:" + "2" * 64),
        model_configuration_digest=RevisionDigest("sha256:" + "3" * 64),
        requested_model_id="demo-model",
        allowed_model_ids=frozenset({"demo-model"}),
        prompt=({"role": "user"}, {"content": "repair fixture"}),
        tool_schema_digest="sha256:" + "4" * 64,
        request_digest="sha256:" + "5" * 64,
        idempotency_key="demo-turn",
        max_input_tokens=128,
        max_output_tokens=64,
        reserved_cost_usd=Decimal(0),
    )
    model = ScriptedMockLLM(
        (
            ScriptedModelStep.for_request(
                request,
                ProviderAttemptResult.completed(
                    ModelCompletion(
                        response_id="demo-response-1",
                        requested_model_id="demo-model",
                        returned_model_id="demo-model",
                        usage=ModelUsage(4, 2, Decimal(0)),
                        normalized_action={"kind": "check", "check_id": "fixture"},
                    )
                ),
            ),
            ScriptedModelStep.for_request(
                request,
                ProviderAttemptResult.completed(
                    ModelCompletion(
                        response_id="demo-response-2",
                        requested_model_id="demo-model",
                        returned_model_id="demo-model",
                        usage=ModelUsage(5, 2, Decimal(0)),
                        normalized_action={"kind": "patch", "path": "src/fix.py"},
                    )
                ),
            ),
        )
    )
    first_completion = model.complete(request)
    feedback = "CHECK_FAILED"
    next_request = replace(request, prompt=({"role": "user"}, {"content": feedback}))
    next_completion = model.complete(next_request)
    if (
        first_completion.completion is None
        or next_completion.completion is None
        or model.call_count != 2
    ):
        raise RuntimeError("DEMO_MODEL_TRACE_INVALID")
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
            "model_calls": "",
            "feedback_bound": "",
        },
        {
            "behavior": "feedback",
            "action": "",
            "decision": "",
            "first_action": str(first_completion.completion.normalized_action["kind"]),
            "next_action": str(next_completion.completion.normalized_action["kind"]),
            "status": feedback,
            "model_calls": str(model.call_count),
            "feedback_bound": next_request.prompt[-1]["content"],
        },
        {
            "behavior": "freshness",
            "action": "",
            "decision": "",
            "first_action": "",
            "next_action": "",
            "status": freshness.status,
            "model_calls": "",
            "feedback_bound": "",
        },
    )


def main() -> None:
    for event in build_demo_trace():
        print(json.dumps(event, sort_keys=True))


if __name__ == "__main__":
    main()
