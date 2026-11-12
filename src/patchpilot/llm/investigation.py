"""Structured investigation output. A citation is a path and a line range, not file text."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchpilot.source.errors import SourceError
from patchpilot.source.paths import relative_parts

Statement = Annotated[str, Field(min_length=1, max_length=400)]
Question = Annotated[str, Field(min_length=1, max_length=300)]
RelativePath = Annotated[str, Field(min_length=1, max_length=300)]
# Stored summaries stay within this length. It is not a JSON Schema maxLength:
# the sampler would otherwise stop the string on the 400th character.
_SUMMARY_LIMIT = 400
# end_line - start_line. JSON Schema cannot express the difference, and a
# maximum on either line number would not stop a wide span.
_MAX_CITATION_SPAN = 40
_SENTENCE_MARKS = ".!?"


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
    start_line: int = Field(
        ge=1,
        le=100_000,
        description="First cited line, inclusive.",
    )
    end_line: int = Field(
        ge=1,
        le=100_000,
        description="Last cited line, inclusive. At most 40 lines after start_line.",
    )
    citation: str = Field(default="", max_length=320)

    @model_validator(mode="after")
    def label_matches_the_span(self) -> Self:
        """Keep a citation inside the span limit and derive its label.

        The schema has no cross-field maximum, so a model can finish a wide
        range. A range that already fits is kept. A wider one keeps start_line
        and stops 40 lines later.
        """
        if any(ord(character) < 32 or character == "\\" for character in self.relative_path):
            raise ValueError("relative_path is not a repository path")
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        if self.end_line - self.start_line > _MAX_CITATION_SPAN:
            self.end_line = self.start_line + _MAX_CITATION_SPAN
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

    summary: str = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=6)
    evidence: list[EvidenceCitation] = Field(default_factory=list, max_length=12)
    probable_files: list[RelativePath] = Field(default_factory=list, max_length=8)
    unanswered_questions: list[Question] = Field(default_factory=list, max_length=8)

    @field_validator("summary", mode="after")
    @classmethod
    def summary_stops_on_a_sentence(cls, value: str) -> str:
        """Keep a stored summary inside 400 characters and on a sentence boundary.

        The schema has no maxLength, so the model can finish the sentence.
        Whitespace stripping still happens first. A summary that already fits
        is kept. A longer one is cut through the last `.`, `!`, or `?` that
        still fits. A `.` between alphanumeric characters, as in `app.py` or
        `README.md`, is not a cut.
        """
        if len(value) <= _SUMMARY_LIMIT:
            return value
        fitted = _summary_through_last_sentence(value, _SUMMARY_LIMIT)
        if fitted is None:
            raise ValueError(f"summary has no complete sentence within {_SUMMARY_LIMIT} characters")
        return fitted


class ToolRequest(BaseModel):
    """One read-only tool call. The path is relative to the registered checkout."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: ToolName
    relative_path: str = Field(
        default=".",
        min_length=1,
        max_length=300,
        description=(
            "Checkout-relative path. For read_text_file this is the file, "
            "such as README.md or src/demo_service/app.py."
        ),
    )
    query: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Search string for search_text only. Leave it empty for a read or a directory listing."
        ),
    )

    @model_validator(mode="after")
    def read_of_the_root_uses_a_path_query(self) -> Self:
        """A read of `.` with a path in `query` is that file, not an invalid path."""
        if self.name is not ToolName.READ_TEXT_FILE or self.query is None:
            return self
        if not _is_checkout_root(self.relative_path):
            return self
        relocated = _relative_file(self.query)
        if relocated is None:
            return self
        self.relative_path = relocated
        self.query = None
        return self


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


def _summary_through_last_sentence(text: str, limit: int) -> str | None:
    """Text through the last sentence ending whose result is within `limit`."""
    last: int | None = None
    length = len(text)
    for index, character in enumerate(text):
        if index >= limit:
            break
        if character not in _SENTENCE_MARKS or not _ends_sentence(text, index, length):
            continue
        last = index + 1
    if last is None:
        return None
    return text[:last]


def _ends_sentence(text: str, index: int, length: int) -> bool:
    """True when `.`, `!`, or `?` is followed by whitespace or the end of `text`."""
    character = text[index]
    if character == "." and _dot_inside_name(text, index, length):
        return False
    following = index + 1
    return following == length or text[following].isspace()


def _dot_inside_name(text: str, index: int, length: int) -> bool:
    """True for a period with a letter or digit on both sides, as in `app.py`."""
    previous = index - 1
    following = index + 1
    if previous < 0 or following >= length:
        return False
    return text[previous].isalnum() and text[following].isalnum()


def _is_checkout_root(value: str) -> bool:
    try:
        return relative_parts(value, allow_root=True) == ()
    except SourceError:
        return False


def _relative_file(value: str) -> str | None:
    try:
        parts = relative_parts(value, allow_root=False)
    except SourceError:
        return None
    return "/".join(parts)
