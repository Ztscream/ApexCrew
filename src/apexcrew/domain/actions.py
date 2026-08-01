from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    ]
    path: str | None = None
    issued_by_admission: bool = False


def delete_action(path: str) -> ActionEnvelope:
    return ActionEnvelope(kind="delete", path=path)


def write_action(path: str) -> ActionEnvelope:
    return ActionEnvelope(kind="patch", path=path)
