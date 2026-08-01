from collections import deque
from collections.abc import Sequence

from apexcrew.domain.model import ModelRequest, ProviderAttemptResult


class ScriptedMockLLM:
    def __init__(self, results: Sequence[ProviderAttemptResult]) -> None:
        self._results = deque(results)
        self.call_count = 0

    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        if not self._results:
            raise AssertionError("unexpected model request")
        self.call_count += 1
        return self._results.popleft()
