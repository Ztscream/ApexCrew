from __future__ import annotations

from pathlib import Path

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.composition import build_application_bundle
from apexcrew.application.configuration import default_revision_documents
from apexcrew.application.control import BootstrapRepositoryAuthority
from apexcrew.domain.commands import ApplicableRevisionDigests, CommandEnvelope, CreateRunPayload
from apexcrew.domain.types import GitOid, RepositoryId


class FixtureRepositoryAuthority:
    def inspect(self, repository_root: str, target_ref: str) -> BootstrapRepositoryAuthority:
        return BootstrapRepositoryAuthority(
            repository_root=repository_root,
            repository_id=RepositoryId("sha256:" + "1" * 64),
            repository_instance_digest="sha256:" + "2" * 64,
            target_ref=target_ref,
            target_oid=GitOid("a" * 40),
        )


def _create_command(root: Path) -> CommandEnvelope:
    revisions = default_revision_documents()
    return CommandEnvelope(
        request_id="production-wiring-create",
        expected_sequence=None,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=CreateRunPayload(
            goal="preserve production bindings",
            constraints=("offline",),
            acceptance_criteria=("reopen",),
            repository_root=str(root),
            target_ref="refs/heads/main",
            expected_target_oid=GitOid("a" * 40),
            policy_revision=revisions.policy,
            budget_revision=revisions.budget,
            model_configuration_revision=revisions.model_configuration.model_copy(
                update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
            ),
        ),
    )


def test_reopened_bundle_preserves_run_bindings(tmp_path: Path) -> None:
    options = {
        "repository_authority": FixtureRepositoryAuthority(),
        "model_configuration": default_revision_documents().model_configuration.model_copy(
            update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
        ),
        "scripted_model": ScriptedMockLLM(()),
    }
    first = build_application_bundle(tmp_path, **options)
    try:
        created = first.control.handle(_create_command(tmp_path))
        assert created.run_id is not None
        before = first.queries.get(created.run_id)
        assert before.run_id == created.run_id
        sequence = before.sequence
    finally:
        first.close()

    second = build_application_bundle(tmp_path, **options)
    try:
        after = second.queries.get(created.run_id)
        assert after.run_id == created.run_id
        assert after.sequence == sequence
        assert after == before
    finally:
        second.close()


def test_production_bundle_uses_concrete_resolution_observer_registry(tmp_path: Path) -> None:
    options = {
        "repository_authority": FixtureRepositoryAuthority(),
        "model_configuration": default_revision_documents().model_configuration.model_copy(
            update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
        ),
        "scripted_model": ScriptedMockLLM(()),
    }
    bundle = build_application_bundle(tmp_path, **options)
    try:
        resolution = bundle.runtime._phase_drivers._resolution  # type: ignore[attr-defined]
        assert type(resolution).__name__ == "ResolutionRuntime"
        assert type(resolution._observer).__name__ == "ResolutionObservationRegistry"
        assert set(resolution._observer._observers) == {  # type: ignore[attr-defined]
            "granted_risky_action",
            "read",
            "search",
            "target_reservation_creation",
        }
    finally:
        bundle.close()
