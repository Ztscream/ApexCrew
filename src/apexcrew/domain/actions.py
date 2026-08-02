from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class ActionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal[
        "read",
        "search",
        "patch",
        "check",
        "finish",
        "fail",
        "delete",
        "rename",
        "chmod_executable",
        "raw_shell",
        "host_access",
        "network",
        "socket",
        "push",
        "reset",
        "clean",
        "force",
        "target_cas",
        "risky_action",
    ]
    path: str | None = None
    issued_by_admission: bool = False


class ReadAction(ActionEnvelope):
    kind: Literal["read"] = "read"
    path: str
    issued_by_admission: Literal[False] = False


class SearchAction(ActionEnvelope):
    kind: Literal["search"] = "search"
    path: None = None
    issued_by_admission: Literal[False] = False
    query: str = Field(min_length=1)
    paths: tuple[str, ...] = Field(min_length=1)


class PatchAction(ActionEnvelope):
    kind: Literal["patch"] = "patch"
    path: str
    issued_by_admission: Literal[False] = False
    unified_diff: str = Field(min_length=1)


class CheckAction(ActionEnvelope):
    kind: Literal["check"] = "check"
    path: None = None
    issued_by_admission: Literal[False] = False
    check_id: str = Field(min_length=1)


class RiskyAction(ActionEnvelope):
    kind: Literal["risky_action"] = "risky_action"
    path: str
    issued_by_admission: Literal[False] = False
    operation: Literal["delete", "rename", "set_executable", "protected_patch"]
    destination: str | None = None
    unified_diff: str | None = None
    executable: bool | None = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        required = {
            "delete": (False, False, False),
            "rename": (True, False, False),
            "set_executable": (False, False, True),
            "protected_patch": (False, True, False),
        }[self.operation]
        observed = (
            self.destination is not None,
            self.unified_diff is not None,
            self.executable is not None,
        )
        if observed != required:
            field_names = ("destination", "unified_diff", "executable")
            expected = (
                ", ".join(
                    name for name, present in zip(field_names, required, strict=True) if present
                )
                or "no operation-specific fields"
            )
            unexpected = ", ".join(
                name
                for name, expected_present, observed_present in zip(
                    field_names, required, observed, strict=True
                )
                if observed_present and not expected_present
            )
            suffix = "" if not unexpected else f"; unexpected {unexpected}"
            raise ValueError(f"{self.operation} requires {expected} only{suffix}")
        return self


class FinishAction(ActionEnvelope):
    kind: Literal["finish"] = "finish"
    path: None = None
    issued_by_admission: Literal[False] = False
    summary: str = Field(min_length=1, max_length=4_096)


class FailAction(ActionEnvelope):
    kind: Literal["fail"] = "fail"
    path: None = None
    issued_by_admission: Literal[False] = False
    reason: str = Field(min_length=1, max_length=4_096)


ToolActionEnvelope = Annotated[
    ReadAction | SearchAction | PatchAction | CheckAction | RiskyAction | FinishAction | FailAction,
    Field(discriminator="kind"),
]
ACTION_ADAPTER: TypeAdapter[ToolActionEnvelope] = TypeAdapter(ToolActionEnvelope)


def delete_action(path: str) -> ActionEnvelope:
    return ActionEnvelope(kind="delete", path=path)


def write_action(path: str) -> ActionEnvelope:
    return ActionEnvelope(kind="patch", path=path)
