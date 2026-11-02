"""Structured triage output. Model text is valid only after this model accepts it."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

AcceptanceCriterion = Annotated[str, Field(min_length=1, max_length=300)]


class Classification(StrEnum):
    BUG = "bug"
    FEATURE = "feature"
    QUESTION = "question"
    UNCLEAR = "unclear"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TriageResult(BaseModel):
    """Classification of one bug report. Extra fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    classification: Classification
    severity: Severity
    summary: str = Field(min_length=1, max_length=400)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool
