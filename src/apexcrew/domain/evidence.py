from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from apexcrew.domain.revisions import FrozenDocument, Sha256DigestText


def _digest(value: object) -> Sha256DigestText:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return Sha256DigestText("sha256:" + sha256(payload.encode("utf-8")).hexdigest())


class ContextCapsule(FrozenDocument):
    schema_version: Literal["context-capsule-v1"] = "context-capsule-v1"
    run_id: str
    task_id: str
    revision_digest: Sha256DigestText
    dependencies: tuple[Sha256DigestText, ...]
    content: str
    content_digest: Sha256DigestText
    binding_digest: Sha256DigestText

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        task_id: str,
        revision_digest: str,
        dependencies: tuple[str, ...],
        content: str,
    ) -> Self:
        content_digest = _digest(content)
        binding = _digest(
            {
                "dependencies": dependencies,
                "revision_digest": revision_digest,
                "content_digest": content_digest,
                "run_id": run_id,
                "task_id": task_id,
            }
        )
        return cls(
            run_id=run_id,
            task_id=task_id,
            revision_digest=revision_digest,
            dependencies=dependencies,
            content=content,
            content_digest=content_digest,
            binding_digest=binding,
        )

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.content_digest != _digest(self.content):
            raise ValueError("CAPSULE_CONTENT_DIGEST_MISMATCH")
        expected = _digest(
            {
                "dependencies": self.dependencies,
                "revision_digest": self.revision_digest,
                "content_digest": self.content_digest,
                "run_id": self.run_id,
                "task_id": self.task_id,
            }
        )
        if self.binding_digest != expected:
            raise ValueError("CAPSULE_BINDING_MISMATCH")
        return self

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.model_validate_json(value)


class EvidenceReceipt(FrozenDocument):
    schema_version: Literal["evidence-receipt-v1"] = "evidence-receipt-v1"
    capsule_binding_digest: Sha256DigestText
    revision_digest: Sha256DigestText
    dependencies: tuple[Sha256DigestText, ...]
    result_class: str
    result_digest: Sha256DigestText
    result: str

    @classmethod
    def create(cls, capsule: ContextCapsule, *, result_class: str, result: str) -> Self:
        valid_capsule = ContextCapsule.model_validate(capsule.model_dump(mode="json"))
        result_digest = _digest({"result": result, "result_class": result_class})
        return cls(
            capsule_binding_digest=valid_capsule.binding_digest,
            revision_digest=valid_capsule.revision_digest,
            dependencies=valid_capsule.dependencies,
            result_class=result_class,
            result_digest=result_digest,
            result=result,
        )

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.result_digest != _digest(
            {"result": self.result, "result_class": self.result_class}
        ):
            raise ValueError("RECEIPT_RESULT_DIGEST_MISMATCH")
        return self

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> Self:
        return cls.model_validate_json(value)
