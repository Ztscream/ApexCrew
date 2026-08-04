from __future__ import annotations

from apexcrew.domain.model import ModelRequest, ProviderAttemptResult


class OpenAIResponsesAdapter:
    """Thin provider seam; offline composition intentionally has no transport."""

    def complete(self, request: ModelRequest) -> ProviderAttemptResult:
        del request
        # DEBT-M4-001: connect an explicitly injected Responses transport after review.
        raise RuntimeError("OPENAI_RESPONSES_DISABLED_OFFLINE")
