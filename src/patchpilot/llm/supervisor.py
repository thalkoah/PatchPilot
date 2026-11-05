"""Structured supervisor recommendation. Policy decides which routes are allowed."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Reason = Annotated[str, Field(min_length=1, max_length=300)]


class SupervisorRoute(StrEnum):
    """A route the graph can take. The model does not choose the node itself."""

    INVESTIGATE = "investigate"
    INVESTIGATE_MORE = "investigate_more"
    PLAN = "plan"
    REPLAN = "replan"
    PROCEED_TO_APPROVAL = "proceed_to_approval"
    PREPARE_WORKSPACE = "prepare_workspace"
    DEVELOP = "develop"
    TEST = "test"
    REVIEW = "review"
    REPAIR = "repair"
    FINALIZE = "finalize"
    ESCALATE = "escalate"


class SupervisorRecommendation(BaseModel):
    """One suggested route. Policy drops it when the route is not allowed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    route: SupervisorRoute
    reason: Reason

    @model_validator(mode="after")
    def reason_is_a_single_line(self) -> Self:
        if any(ord(character) < 32 for character in self.reason):
            raise ValueError("reason is not a single line")
        return self
