from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from apexcrew.adapters.model.scripted import ScriptedMockLLM
from apexcrew.application.configuration import default_revision_documents
from apexcrew.application.control import BootstrapRepositoryAuthority
from apexcrew.domain.commands import (
    ApplicableRevisionDigests,
    CommandEnvelope,
    CreateRunPayload,
)
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


def _bundle_factory():
    module = (
        importlib.import_module("apexcrew.application.composition")
        if importlib.util.find_spec("apexcrew.application.composition") is not None
        else None
    )
    assert module is not None, "production composition module is missing"
    factory = getattr(module, "build_application_bundle", None)
    assert factory is not None, "production bundle factory is missing"
    return factory


def _create_command(root: Path) -> CommandEnvelope:
    revisions = default_revision_documents()
    return CommandEnvelope(
        request_id="composition-create",
        expected_sequence=None,
        applicable_revision_digests=ApplicableRevisionDigests(),
        payload=CreateRunPayload(
            goal="compose one production object graph",
            constraints=("offline",),
            acceptance_criteria=("shared state",),
            repository_root=str(root),
            target_ref="refs/heads/main",
            expected_target_oid=GitOid("a" * 40),
            policy_revision=revisions.policy,
            budget_revision=revisions.budget,
            model_configuration_revision=revisions.model_configuration.model_copy(
                update={
                    "provider": "scripted_mock",
                    "provider_base_origin": "mock://scripted",
                }
            ),
        ),
    )


def test_bundle_shares_one_state_store(tmp_path: Path) -> None:
    factory = _bundle_factory()
    bundle = factory(
        tmp_path,
        repository_authority=FixtureRepositoryAuthority(),
        model_configuration=_create_command(tmp_path).payload.model_configuration_revision,
        scripted_model=ScriptedMockLLM(()),
    )
    try:
        outcome = bundle.control.handle(_create_command(tmp_path))
        assert outcome.status == "ACCEPTED"
        assert outcome.run_id is not None

        before = bundle.queries.get(outcome.run_id)
        stop = bundle.runtime.run_until_blocked(outcome.run_id)
        after = bundle.queries.get(outcome.run_id)

        assert before.availability == "AVAILABLE"
        assert stop.reason == "NO_RUNTIME_PERMIT"
        assert after.availability == "AVAILABLE"
        assert after.run_id == outcome.run_id
        assert after.sequence == before.sequence
    finally:
        bundle.close()


def test_bundle_close_attempts_all_resources_after_failure() -> None:
    module = importlib.import_module("apexcrew.application.composition")
    bundle_type = module.ApplicationBundle
    closed: list[str] = []

    class FailingClose:
        def close(self) -> None:
            closed.append("failing")
            raise RuntimeError("close failed")

    class RecordingClose:
        def close(self) -> None:
            closed.append("recording")

    bundle = bundle_type(
        control=object(),
        runtime=object(),
        queries=object(),
        closeables=(RecordingClose(), FailingClose()),
    )

    try:
        bundle.close()
    except RuntimeError as error:
        assert str(error) == "close failed"
    else:
        raise AssertionError("bundle cleanup should report the first close failure")

    assert closed == ["failing", "recording"]
