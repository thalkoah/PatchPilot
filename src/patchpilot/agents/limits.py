"""Hard ceiling for investigator tool calls.

A setting or a caller can choose a smaller budget. Values above the ceiling are
rejected so the limit cannot be turned off.
"""

DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS = 6
HARD_MAX_INVESTIGATOR_TOOL_CALLS = 12


def require_tool_budget(value: int) -> int:
    """Return the budget, or raise when it is not a small positive integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max tool calls must be an integer")
    if not 1 <= value <= HARD_MAX_INVESTIGATOR_TOOL_CALLS:
        raise ValueError(f"max tool calls must be from 1 to {HARD_MAX_INVESTIGATOR_TOOL_CALLS}")
    return value
