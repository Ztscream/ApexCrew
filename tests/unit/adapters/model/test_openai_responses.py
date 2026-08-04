from __future__ import annotations

import pytest

from apexcrew.adapters.model.openai_responses import OpenAIResponsesAdapter


def test_openai_adapter_is_thin_and_disabled_without_explicit_provider_transport() -> None:
    adapter = OpenAIResponsesAdapter()
    with pytest.raises(RuntimeError, match="OPENAI_RESPONSES_DISABLED_OFFLINE"):
        adapter.complete(object())
