from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Self

from apexcrew.domain.commands import CreateRunPayload
from apexcrew.domain.revisions import (
    BudgetRevisionDocument,
    ExecutorProfileDocument,
    HardDeniedPathClass,
    InferenceSettingsDocument,
    ModelConfigurationRevisionDocument,
    ModelPricingEntryDocument,
    PlanningReadAuthorizationDocument,
    PolicyRevisionDocument,
    ReturnedModelAliasDocument,
    SecretPathBindingDocument,
    Sha256DigestText,
    ToolVersionDocument,
)
from apexcrew.domain.types import GitOid


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RunOptions:
    goal: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    repository_root: Path
    target_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str):
            raise ConfigurationError("goal must be text")
        if not isinstance(self.target_ref, str):
            raise ConfigurationError("target_ref must be text")
        if not isinstance(self.constraints, tuple) or not all(
            isinstance(value, str) for value in self.constraints
        ):
            raise ConfigurationError("constraints must be a tuple of text")
        if not isinstance(self.acceptance_criteria, tuple) or not all(
            isinstance(value, str) for value in self.acceptance_criteria
        ):
            raise ConfigurationError("acceptance_criteria must be a tuple of text")
        if not isinstance(self.repository_root, Path):
            raise ConfigurationError("repository_root must be a path")
        object.__setattr__(self, "repository_root", self.repository_root.resolve())
        if not self.goal or self.goal != self.goal.strip():
            raise ConfigurationError("goal must be nonblank and trimmed")
        if not self.target_ref or self.target_ref != self.target_ref.strip():
            raise ConfigurationError("target_ref must be nonblank and trimmed")
        for field_name, values in (
            ("constraints", self.constraints),
            ("acceptance_criteria", self.acceptance_criteria),
        ):
            if any(not value or value != value.strip() for value in values):
                raise ConfigurationError(f"{field_name} entries must be nonblank and trimmed")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> Self:
        expected = {
            "goal",
            "constraints",
            "acceptance_criteria",
            "repository_root",
            "target_ref",
        }
        unknown = set(values) - expected
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ConfigurationError(f"unknown configuration key: {names}")
        missing = expected - set(values)
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigurationError(f"missing configuration key: {names}")
        return cls(
            goal=_text_value(values["goal"], "goal"),
            constraints=_text_sequence(values["constraints"], "constraints"),
            acceptance_criteria=_text_sequence(
                values["acceptance_criteria"], "acceptance_criteria"
            ),
            repository_root=_path_value(values["repository_root"]),
            target_ref=_text_value(values["target_ref"], "target_ref"),
        )


@dataclass(frozen=True, slots=True)
class RevisionDocuments:
    policy: PolicyRevisionDocument
    budget: BudgetRevisionDocument
    model_configuration: ModelConfigurationRevisionDocument


def default_revision_documents() -> RevisionDocuments:
    zero_digest = Sha256DigestText("sha256:" + "0" * 64)
    return RevisionDocuments(
        policy=PolicyRevisionDocument(
            schema_version="policy-revision-v1",
            planning_read_authorization=PlanningReadAuthorizationDocument(
                matcher_version="apexcrew-path-v1",
                positive_globs=("src/**",),
                hard_denied_path_classes=tuple(HardDeniedPathClass),
                max_manifest_entries=2_000,
                max_manifest_bytes=131_072,
                max_file_bytes=131_072,
                max_total_returned_bytes=2_097_152,
                max_search_matches=200,
                max_search_bytes=65_536,
            ),
            secret_path_binding=SecretPathBindingDocument(
                defaults_version="secret-path-defaults-v1",
                matcher_version="apexcrew-path-v1",
                rules_hmac=zero_digest,
                user_rule_count=0,
            ),
            executor_profile=ExecutorProfileDocument(
                image_digest=zero_digest,
                platform="linux",
                architecture="x86_64",
                tool_versions=(ToolVersionDocument(name="python", version="3.12"),),
                allowed_executables=("python",),
                environment_allowlist=("LC_ALL",),
                run_as_uid=1000,
                run_as_gid=1000,
                root_filesystem_read_only=True,
                network_mode="none",
                cpu_limit=Decimal(2),
                memory_limit_bytes=2_147_483_648,
                pids_limit=256,
                scratch_limit_bytes=536_870_912,
                drop_all_capabilities=True,
                no_new_privileges=True,
            ),
            action_policy="default-action-policy-v1",
            grant_ttl_seconds=600,
        ),
        budget=BudgetRevisionDocument(
            schema_version="budget-revision-v1",
            active_run_seconds_ceiling=28_800,
            task_ceiling=12,
            planning_request_ceiling=8,
            model_call_ceiling=240,
            input_token_ceiling=2_000_000,
            output_token_ceiling=200_000,
            cost_reserve_usd=Decimal(10),
            concurrent_worker_ceiling=3,
            pricing_observed_on=date(2026, 8, 5),
            pricing_entries=(
                ModelPricingEntryDocument(
                    returned_model_id="deepseek-v4-flash",
                    input_usd_per_million=Decimal("0.28"),
                    output_usd_per_million=Decimal("0.56"),
                ),
            ),
        ),
        model_configuration=ModelConfigurationRevisionDocument(
            schema_version="model-configuration-revision-v1",
            provider="deepseek_responses",
            provider_base_origin="https://api.deepseek.com",
            requested_model_id="deepseek-v4-flash",
            returned_model_aliases=(
                ReturnedModelAliasDocument(
                    returned_model_id="deepseek-v4-flash",
                    canonical_model_id="deepseek-v4-flash",
                ),
            ),
            inference_settings=InferenceSettingsDocument(
                max_input_tokens=32_000,
                max_output_tokens=4_096,
                provider_storage_enabled=False,
            ),
            tool_schema_digest=zero_digest,
        ),
    )


def build_create_run_payload(
    options: RunOptions,
    *,
    target_oid: GitOid,
    revisions: RevisionDocuments | None = None,
) -> CreateRunPayload:
    current = default_revision_documents() if revisions is None else revisions
    return CreateRunPayload(
        goal=options.goal,
        constraints=options.constraints,
        acceptance_criteria=options.acceptance_criteria,
        repository_root=str(options.repository_root),
        target_ref=options.target_ref,
        expected_target_oid=target_oid,
        policy_revision=current.policy,
        budget_revision=current.budget,
        model_configuration_revision=current.model_configuration,
    )


def _text_value(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{field_name} must be text")
    return value


def _text_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ConfigurationError(f"{field_name} must be a sequence of text")
    values = tuple(value)
    if any(not isinstance(item, str) for item in values):
        raise ConfigurationError(f"{field_name} must be a sequence of text")
    return values


def _path_value(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise ConfigurationError("repository_root must be a path")
    return Path(value)
