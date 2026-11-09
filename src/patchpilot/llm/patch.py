"""Structured patch proposal. This is model output, not the applied diff."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from patchpilot.llm.limits import MAX_MODEL_CONTENT_CHARS

Summary = Annotated[str, Field(min_length=1, max_length=400)]
RelativePath = Annotated[str, Field(min_length=1, max_length=300)]
# Large enough that the apply tool, not the schema, rejects an overlong diff.
FileContent = Annotated[str, Field(min_length=1, max_length=20_000)]
UnifiedDiff = Annotated[str, Field(max_length=20_000)]
# A maxLength on old or new is enforced by the response grammar as a hard cut.
# That cut a function off in the middle of a dict, and the file then failed to
# parse. The whole JSON object is still rejected above the parser cap. This
# size is only how large a fixture span may be.
DEVELOP_MAX_EDITS = 3
DEVELOP_PATH_MAX = 120
_DEVELOP_EDIT = '{"relative_path":"","old":"","new":""}'
_DEVELOP_JSON_WRAPPER = len(
    '{"summary":"","edits":[' + ",".join([_DEVELOP_EDIT] * DEVELOP_MAX_EDITS) + "]}"
)
_SUMMARY_ESCAPE_RESERVE = 400 * 2
_PATH_ESCAPE_RESERVE = DEVELOP_MAX_EDITS * DEVELOP_PATH_MAX * 2
_SNIPPET_FIELDS = DEVELOP_MAX_EDITS * 2
DEVELOP_SNIPPET_MAX_CHARS = (
    MAX_MODEL_CONTENT_CHARS - _DEVELOP_JSON_WRAPPER - _SUMMARY_ESCAPE_RESERVE - _PATH_ESCAPE_RESERVE
) // (2 * _SNIPPET_FIELDS)
DevelopPath = Annotated[str, Field(min_length=1, max_length=DEVELOP_PATH_MAX)]
DevelopSnippet = str


class FileEdit(BaseModel):
    """One full-file replacement. `expected_sha256` is the current file, or empty when new."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    relative_path: RelativePath
    expected_sha256: str
    content: FileContent

    @field_validator("expected_sha256")
    @classmethod
    def hash_is_empty_or_sha256(cls, value: str) -> str:
        if value == "":
            return value
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("expected_sha256 is not a sha256")
        return value

    @model_validator(mode="after")
    def path_is_a_single_line(self) -> Self:
        if any(ord(character) < 32 or character == "\\" for character in self.relative_path):
            raise ValueError("relative_path is not a repository path")
        return self


class SnippetEdit(BaseModel):
    """One exact replacement. `old` is empty only when the test file is new."""

    model_config = ConfigDict(extra="forbid")

    relative_path: DevelopPath
    old: DevelopSnippet
    new: DevelopSnippet

    @field_validator("relative_path")
    @classmethod
    def path_is_a_single_line(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 or character == "\\" for character in value):
            raise ValueError("relative_path is not a repository path")
        return value


class DevelopDiff(BaseModel):
    """The object the develop model fills. Exact replacements, not a git diff."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: Summary
    edits: list[SnippetEdit] = Field(min_length=1, max_length=DEVELOP_MAX_EDITS)


class PatchProposal(BaseModel):
    """What the model proposed. The applied artifact is a separate unified diff."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: Summary
    edits: list[FileEdit] = Field(default_factory=list, max_length=8)
    unified_diff: UnifiedDiff = ""

    @model_validator(mode="after")
    def one_patch_form(self) -> Self:
        has_edits = len(self.edits) > 0
        has_diff = bool(self.unified_diff.strip())
        if has_edits == has_diff:
            raise ValueError("proposal must be file edits or one unified diff")
        return self
