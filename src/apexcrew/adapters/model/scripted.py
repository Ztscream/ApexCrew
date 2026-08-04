from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from apexcrew.domain.model import ModelRequest, ProviderAttemptResult


@dataclass(frozen=True, slots=True)
class ScriptedModelStep:
    request_digest: str
    idempotency_key: str
    requested_model_id: str
    result: ProviderAttemptResult

    @classmethod
    def for_request(
        cls,
        request: ModelRequest,
        result: ProviderAttemptResult,
    ) -> "ScriptedModelStep":
        return cls(
            request_digest=request.request_digest,
            idempotency_key=request.idempotency_key,
            requested_model_id=request.requested_model_id,
            result=result,
        )


class ScriptedMockLLM:
    def __init__(self, results: Sequence[ScriptedModelStep]) -> None:
        if any(not isinstance(result, ScriptedModelStep) for result in results):
            raise TypeError("SCRIPTED_MODEL_STEP_REQUIRED")
        self._results = deque(results)
        self.call_count = 0

    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        if not self._results:
            raise AssertionError("unexpected model request")
        step = self._results[0]
        if (
            step.request_digest != request.request_digest
            or step.idempotency_key != request.idempotency_key
            or step.requested_model_id != request.requested_model_id
        ):
            raise AssertionError("SCRIPTED_MODEL_REQUEST_BINDING_MISMATCH")
        self._results.popleft()
        self.call_count += 1
        return step.result
