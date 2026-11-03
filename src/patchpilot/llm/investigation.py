"""Structured investigation output. A citation is a path and a line range, not file text."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Statement = Annotated[str, Field(min_length=1, max_length=400)]
Question = Annotated[str, Field(min_length=1, max_length=300)]
RelativePath = Annotated[str, Field(min_length=1, max_length=300)]


class ToolName(StrEnum):
    LIST_TREE = "list_tree"
    READ_TEXT_FILE = "read_text_file"
    SEARCH_TEXT = "search_text"


def citation_label(relative_path: str, start_line: int, end_line: int) -> str:
    """`relative_path:line` or `relative_path:start-end`."""
    if start_line == end_line:
        return f"{relative_path}:{start_line}"
    return f"{relative_path}:{start_line}-{end_line}"


class EvidenceCitation(BaseModel):
    """One span the model claims to have read. The label is derived from the numbers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    relative_path: RelativePath
    start_line: int = Field(ge=1, le=100_000)
    end_line: int = Field(ge=1, le=100_000)
    citation: str = Field(default="", max_length=320)

    @model_validator(mode="after")
    def label_matches_the_span(self) -> Self:
        if any(ord(character) < 32 or character == "\\" for character in self.relative_path):
            raise ValueError("relative_path is not a repository path")
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        if self.end_line - self.start_line > 40:
            raise ValueError("line range is too wide")
        self.citation = citation_label(self.relative_path, self.start_line, self.end_line)
        return self


class Hypothesis(BaseModel):
    """One explanation. Its citations are the only support the report keeps for it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    statement: Statement
    citations: list[EvidenceCitation] = Field(default_factory=list, max_length=8)


class InvestigationReport(BaseModel):
    """What the investigator may store. File bodies are not part of this object."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: Statement
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=6)
    evidence: list[EvidenceCitation] = Field(default_factory=list, max_length=12)
    probable_files: list[RelativePath] = Field(default_factory=list, max_length=8)
    unanswered_questions: list[Question] = Field(default_factory=list, max_length=8)


class ToolRequest(BaseModel):
    """One read-only tool call. The path is relative to the registered checkout."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: ToolName
    relative_path: str = Field(default=".", min_length=1, max_length=300)
    query: str | None = Field(default=None, max_length=200)


class InvestigatorStep(BaseModel):
    """The model either asks for one tool or returns the report. Not both."""

    model_config = ConfigDict(extra="forbid")

    tool: ToolRequest | None = None
    report: InvestigationReport | None = None

    @model_validator(mode="after")
    def exactly_one_action(self) -> Self:
        if (self.tool is None) == (self.report is None):
            raise ValueError("exactly one of tool or report is required")
        return self
