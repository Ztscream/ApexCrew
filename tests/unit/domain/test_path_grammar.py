import pytest

from apexcrew.domain.plan import (
    CanonicalPath,
    GlobPattern,
    GlobProof,
    GlobValidationError,
    PathValidationError,
    prove_disjoint,
    prove_included,
)


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "/absolute/path.py",
        "D:outside",
        "D:/outside",
        "//server/share/path.py",
        r"\\server\share\path.py",
        r"src\main.py",
        "src/./main.py",
        "src/../main.py",
        "src/\x00name.py",
        "src/.GIT/config",
        "src/CON.txt",
        "src/name. ",
    ],
)
def test_invalid_or_protected_paths_are_rejected(path: str) -> None:
    with pytest.raises(PathValidationError):
        CanonicalPath.parse(path)


def test_glob_uses_segment_star_and_complete_double_star() -> None:
    assert GlobPattern.parse("src/*.py").matches(CanonicalPath.parse("src/.hidden.py"))
    assert not GlobPattern.parse("src/*.py").matches(CanonicalPath.parse("src/pkg/main.py"))
    assert GlobPattern.parse("src/**").matches(CanonicalPath.parse("src/pkg/main.py"))
    with pytest.raises(GlobValidationError):
        GlobPattern.parse("src/**x.py")


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        ".",
        "..",
        "/absolute/**",
        "D:outside/**",
        "D:/outside/**",
        "//server/share/**",
        r"\\server\share\**",
        r"src\*.py",
        "src/./*.py",
        "src/../*.py",
        "src/\x00*.py",
    ],
)
def test_glob_rejects_noncanonical_path_forms(pattern: str) -> None:
    with pytest.raises(GlobValidationError):
        GlobPattern.parse(pattern)


def test_glob_proofs_are_conservative_and_explicit() -> None:
    literal = GlobPattern.parse("src/pricing.py")
    assert prove_included(literal, GlobPattern.parse("src/**")) is GlobProof.PROVEN
    assert (
        prove_disjoint(GlobPattern.parse("src/**"), GlobPattern.parse("docs/**"))
        is GlobProof.PROVEN
    )
    assert (
        prove_disjoint(GlobPattern.parse("src/*.py"), GlobPattern.parse("src/*ing.py"))
        is GlobProof.UNKNOWN
    )
