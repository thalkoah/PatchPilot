"""Structured review. The reviewer reports a verdict and does not edit files."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Statement = Annotated[str, Field(min_length=1, max_length=400)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=300)]


class ReviewAction(StrEnum):
    """What the reviewer recommends. Policy may ignore this recommendation."""

    ACCEPT = "accept"
    REPAIR = "repair"
    ESCALATE = "escalate"


class FindingSeverity(StrEnum):
    """How serious one finding is. `critical` blocks a successful finish."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewFinding(BaseModel):
    """One observation. `evidence` must refer to the diff or the test report."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: Statement
    severity: FindingSeverity
    evidence: EvidenceText

    @model_validator(mode="after")
    def evidence_is_a_single_line(self) -> Self:
        if any(ord(character) < 32 for character in self.evidence):
            raise ValueError("evidence is not a single line")
        return self


class ReviewResult(BaseModel):
    """The reviewer recommendation. It is not permission to change the worktree."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: Statement
    action: ReviewAction
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=8)
    evidence: list[EvidenceText] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def text_is_single_line(self) -> Self:
        if any(any(ord(character) < 32 for character in item) for item in self.evidence):
            raise ValueError("evidence is not a single line")
        return self
