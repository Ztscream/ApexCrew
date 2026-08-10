from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apexcrew.domain.types import RevisionDigest

Sha256DigestText = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class HardDeniedPathClass(StrEnum):
    OUTSIDE_POSITIVE_SCOPE = "OUTSIDE_POSITIVE_SCOPE"
    UNTRACKED_OR_IGNORED = "UNTRACKED_OR_IGNORED"
    SYMLINK_OR_REPARSE = "SYMLINK_OR_REPARSE"
    PROTECTED_CONTROL_PATH = "PROTECTED_CONTROL_PATH"
    EFFECTIVE_SECRET_PATH = "EFFECTIVE_SECRET_PATH"


class FrozenDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_trimmed_nonblank(value: str, field_name: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be nonblank and trimmed")


def _require_sorted_unique_non_empty(values: tuple[str, ...], field_name: str) -> None:
    if not values:
        raise ValueError(f"{field_name} must be non-empty")
    if values != tuple(sorted(values)):
        raise ValueError(f"{field_name} must be sorted")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must be duplicate-free")
    for value in values:
        _require_trimmed_nonblank(value, field_name)


class PlanningReadAuthorizationDocument(FrozenDocument):
    matcher_version: Literal["apexcrew-path-v1"]
    positive_globs: tuple[str, ...]
    hard_denied_path_classes: tuple[HardDeniedPathClass, ...]
    max_manifest_entries: int = Field(ge=1, le=2_000)
    max_manifest_bytes: int = Field(ge=1, le=131_072)
    max_file_bytes: int = Field(ge=1, le=131_072)
    max_total_returned_bytes: int = Field(ge=1, le=2_097_152)
    max_search_matches: int = Field(ge=1, le=200)
    max_search_bytes: int = Field(ge=1, le=65_536)

    @model_validator(mode="after")
    def validate_canonical_scope(self) -> Self:
        _require_sorted_unique_non_empty(self.positive_globs, "positive_globs")
        required = tuple(HardDeniedPathClass)
        if self.hard_denied_path_classes != required:
            raise ValueError(
                "hard_denied_path_classes must contain all five classes once in declared enum order"
            )
        return self


class SecretPathBindingDocument(FrozenDocument):
    defaults_version: Literal["secret-path-defaults-v1"]
    matcher_version: Literal["apexcrew-path-v1"]
    rules_hmac: Sha256DigestText
    user_rule_count: int = Field(ge=0)


class ToolVersionDocument(FrozenDocument):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        _require_trimmed_nonblank(self.name, "tool name")
        _require_trimmed_nonblank(self.version, "tool version")
        return self


class ExecutorProfileDocument(FrozenDocument):
    image_digest: Sha256DigestText
    platform: Literal["linux"]
    architecture: Literal["x86_64"]
    tool_versions: tuple[ToolVersionDocument, ...]
    allowed_executables: tuple[str, ...]
    environment_allowlist: tuple[str, ...]
    run_as_uid: int = Field(ge=1)
    run_as_gid: int = Field(ge=1)
    root_filesystem_read_only: Literal[True]
    network_mode: Literal["none"]
    cpu_limit: Decimal = Field(gt=0, le=Decimal(2))
    memory_limit_bytes: int = Field(ge=1, le=2_147_483_648)
    pids_limit: int = Field(ge=1, le=256)
    scratch_limit_bytes: int = Field(ge=1, le=536_870_912)
    drop_all_capabilities: Literal[True]
    no_new_privileges: Literal[True]

    @model_validator(mode="after")
    def validate_canonical_lists(self) -> Self:
        _require_sorted_unique_non_empty(
            tuple(tool.name for tool in self.tool_versions), "tool_versions"
        )
        _require_sorted_unique_non_empty(self.allowed_executables, "allowed_executables")
        _require_sorted_unique_non_empty(self.environment_allowlist, "environment_allowlist")
        return self


class PolicyRevisionDocument(FrozenDocument):
    schema_version: Literal["policy-revision-v1"]
    planning_read_authorization: PlanningReadAuthorizationDocument
    secret_path_binding: SecretPathBindingDocument
    executor_profile: ExecutorProfileDocument
    action_policy: Literal["default-action-policy-v1"]
    grant_ttl_seconds: int = Field(ge=1, le=1_800)


class ModelPricingEntryDocument(FrozenDocument):
    returned_model_id: str = Field(min_length=1)
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_returned_model_id(self) -> Self:
        _require_trimmed_nonblank(self.returned_model_id, "returned_model_id")
        return self


class BudgetRevisionDocument(FrozenDocument):
    schema_version: Literal["budget-revision-v1"]
    active_run_seconds_ceiling: int = Field(ge=1, le=28_800)
    task_ceiling: int = Field(ge=1, le=12)
    planning_request_ceiling: int = Field(ge=1, le=8)
    model_call_ceiling: int = Field(ge=1, le=240)
    input_token_ceiling: int = Field(ge=1, le=2_000_000)
    output_token_ceiling: int = Field(ge=1, le=200_000)
    cost_reserve_usd: Decimal = Field(ge=0, le=Decimal(10))
    concurrent_worker_ceiling: int = Field(ge=1, le=3)
    pricing_observed_on: date
    pricing_entries: tuple[ModelPricingEntryDocument, ...]

    @model_validator(mode="after")
    def validate_pricing_entries(self) -> Self:
        _require_sorted_unique_non_empty(
            tuple(entry.returned_model_id for entry in self.pricing_entries),
            "pricing_entries",
        )
        return self


class InferenceSettingsDocument(FrozenDocument):
    max_input_tokens: int = Field(ge=1, le=32_000)
    max_output_tokens: int = Field(ge=1, le=4_096)
    temperature: float = Field(ge=0, le=2)
    reasoning_effort: Literal["low", "medium", "high"]
    provider_storage_enabled: Literal[False]


class ReturnedModelAliasDocument(FrozenDocument):
    returned_model_id: str = Field(min_length=1)
    canonical_model_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        _require_trimmed_nonblank(self.returned_model_id, "returned_model_id")
        _require_trimmed_nonblank(self.canonical_model_id, "canonical_model_id")
        return self


class ModelConfigurationRevisionDocument(FrozenDocument):
    schema_version: Literal["model-configuration-revision-v1"]
    provider: Literal["scripted_mock", "deepseek_responses"]
    provider_base_origin: Literal["mock://scripted", "https://api.deepseek.com"]
    requested_model_id: str = Field(min_length=1)
    returned_model_aliases: tuple[ReturnedModelAliasDocument, ...]
    inference_settings: InferenceSettingsDocument
    tool_schema_digest: Sha256DigestText

    @model_validator(mode="after")
    def validate_provider_and_aliases(self) -> Self:
        _require_trimmed_nonblank(self.requested_model_id, "requested_model_id")
        alias_ids = tuple(alias.returned_model_id for alias in self.returned_model_aliases)
        _require_sorted_unique_non_empty(alias_ids, "returned_model_aliases")
        if any(
            alias.canonical_model_id != self.requested_model_id
            for alias in self.returned_model_aliases
        ):
            raise ValueError("every alias target must equal requested_model_id")
        required_origin = {
            "scripted_mock": "mock://scripted",
            "deepseek_responses": "https://api.deepseek.com",
        }[self.provider]
        if self.provider_base_origin != required_origin:
            raise ValueError("provider and provider_base_origin must match exactly")
        return self


def revision_digest(document: FrozenDocument) -> RevisionDigest:
    canonical_bytes = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RevisionDigest("sha256:" + sha256(canonical_bytes).hexdigest())
