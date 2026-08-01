from collections import deque
from collections.abc import Sequence

from apexcrew.domain.model import ModelCompletion, ModelRequest


class ScriptedMockLLM:
    def __init__(self, completions: Sequence[ModelCompletion]) -> None:
        self._completions = deque(completions)

    def complete(self, request: ModelRequest) -> ModelCompletion:
        if not self._completions:
            raise AssertionError("unexpected model request")
        return self._completions.popleft()
