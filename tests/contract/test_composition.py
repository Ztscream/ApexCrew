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
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            closed.append("failing")
            if self.attempts == 1:
                raise RuntimeError("close failed")

    class RecordingClose:
        def close(self) -> None:
            closed.append("recording")

    failing = FailingClose()
    bundle = bundle_type(
        control=object(),
        runtime=object(),
        queries=object(),
        closeables=(RecordingClose(), failing),
    )

    try:
        bundle.close()
    except RuntimeError as error:
        assert str(error) == "close failed"
    else:
        raise AssertionError("bundle cleanup should report the first close failure")

    assert closed == ["failing", "recording"]
    bundle.close()
    assert closed == ["failing", "recording", "failing"]
    bundle.close()
    assert closed == ["failing", "recording", "failing"]


def _assert_no_deferred_boundary(
    value: object, seen: set[int] | None = None, depth: int = 0
) -> None:
    if seen is None:
        seen = set()
    if depth > 10 or id(value) in seen:
        return
    seen.add(id(value))
    assert not type(value).__name__.startswith("_Deferred"), type(value).__name__
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            _assert_no_deferred_boundary(item, seen, depth + 1)
        return
    for name in getattr(value, "__slots__", ()):
        if isinstance(name, str) and hasattr(value, name):
            _assert_no_deferred_boundary(getattr(value, name), seen, depth + 1)
    for item in getattr(value, "__dict__", {}).values():
        _assert_no_deferred_boundary(item, seen, depth + 1)


def test_bundle_exposes_only_public_interfaces_and_no_deferred_graph(tmp_path: Path) -> None:
    factory = _bundle_factory()
    revisions = default_revision_documents()
    bundle = factory(
        tmp_path,
        repository_authority=FixtureRepositoryAuthority(),
        model_configuration=revisions.model_configuration.model_copy(
            update={"provider": "scripted_mock", "provider_base_origin": "mock://scripted"}
        ),
        scripted_model=ScriptedMockLLM(()),
    )
    try:
        assert set(bundle.__slots__) == {
            "_closeables",
            "_closed",
            "_closed_indices",
            "control",
            "queries",
            "runtime",
        }
        _assert_no_deferred_boundary(bundle.control)
        _assert_no_deferred_boundary(bundle.runtime)
        _assert_no_deferred_boundary(bundle.queries)
    finally:
        bundle.close()
