"""Structured change proposal. The planner describes paths; it does not write them."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Statement = Annotated[str, Field(min_length=1, max_length=400)]
RelativePath = Annotated[str, Field(min_length=1, max_length=300)]


class ModificationKind(StrEnum):
    """Where a proposed path is allowed to point."""

    EXISTING = "existing"
    NEW_TEST = "new_test"


class ProposedModification(BaseModel):
    """One file the planner wants a later step to change. This is not a diff."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    relative_path: RelativePath
    kind: ModificationKind
    summary: Statement

    @model_validator(mode="after")
    def path_is_a_single_line(self) -> Self:
        if any(ord(character) < 32 or character == "\\" for character in self.relative_path):
            raise ValueError("relative_path is not a repository path")
        return self


class ChangePlan(BaseModel):
    """A reviewable proposal. `can_proceed` is false when the evidence is not enough."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    problem_statement: Statement
    assumptions: list[Statement] = Field(default_factory=list, max_length=8)
    modifications: list[ProposedModification] = Field(default_factory=list, max_length=8)
    test_strategy: Statement
    risks: list[Statement] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    can_proceed: bool
